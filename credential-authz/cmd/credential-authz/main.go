// Command credential-authz is an Envoy ext_authz gRPC service that injects the
// Anthropic API key into outbound requests as the x-api-key header.
//
// It loads the API key from a Kubernetes Secret mounted as a file (typically
// /etc/anthropic-secret/api-key) and uses fsnotify to react to Secret rotation
// without polling or restart.
//
// Architecture (see ARCHITECTURE.md "Credential Injection — Istio + ext_authz"):
//
//	agent-task pod
//	   ↓ HTTP request to api.anthropic.com
//	Istio sidecar (Envoy)
//	   ↓ gRPC ext_authz CheckRequest
//	credential-authz   ←  reads /etc/anthropic-secret/api-key (kubelet-projected)
//	   ↓ CheckResponse with x-api-key header
//	Envoy adds header, originates TLS, forwards to Anthropic
//
// The Secret itself is created out-of-band via `kubectl create secret` so that
// the API key never enters Helm templating context, ArgoCD Application status,
// or any other state surface besides the K8s Secret object.
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	authv3 "github.com/envoyproxy/go-control-plane/envoy/service/auth/v3"
	"google.golang.org/grpc"

	"github.com/subganapathy/k8s-debug-agents/credential-authz/internal/secret"
	"github.com/subganapathy/k8s-debug-agents/credential-authz/internal/server"
)

func main() {
	var (
		grpcAddr   = flag.String("grpc-addr", ":9001", "gRPC listen address (ext_authz endpoint)")
		healthAddr = flag.String("health-addr", ":9002", "HTTP listen address (/healthz, /readyz)")
		secretPath = flag.String("secret-path", "/etc/anthropic-secret/api-key", "Path to the API key file (kubelet-projected K8s Secret)")
		logLevel   = flag.String("log-level", "info", "slog level: debug|info|warn|error")
	)
	flag.Parse()

	if err := setupLogging(*logLevel); err != nil {
		fmt.Fprintf(os.Stderr, "log setup failed: %v\n", err)
		os.Exit(2)
	}

	slog.Info("credential-authz starting",
		"grpc_addr", *grpcAddr,
		"health_addr", *healthAddr,
		"secret_path", *secretPath,
	)

	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer cancel()

	// Secret loader uses an fsnotify watcher on the parent directory. Kubernetes
	// Secret-mount semantics: kubelet writes to a hidden ..data dir then atomically
	// re-symlinks. Watching the directory (not the file) catches the rename.
	loader := secret.NewLoader(*secretPath)
	go func() {
		if err := loader.Watch(ctx); err != nil && !errors.Is(err, context.Canceled) {
			slog.Error("secret watcher exited", "err", err)
		}
	}()

	// gRPC server implementing Envoy ext_authz v3.
	srv := server.New(loader)
	grpcSrv := grpc.NewServer()
	authv3.RegisterAuthorizationServer(grpcSrv, srv)

	lis, err := net.Listen("tcp", *grpcAddr)
	if err != nil {
		slog.Error("grpc listen failed", "addr", *grpcAddr, "err", err)
		os.Exit(1)
	}
	go func() {
		slog.Info("grpc server listening", "addr", *grpcAddr)
		if err := grpcSrv.Serve(lis); err != nil {
			slog.Error("grpc server failed", "err", err)
		}
	}()

	// HTTP health endpoints. /readyz returns 503 until the loader has its first
	// successful read of the API key, so the pod stays NotReady until the Secret
	// exists. This lets you bring up the chart first and create the Secret later.
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok\n"))
	})
	mux.HandleFunc("/readyz", func(w http.ResponseWriter, _ *http.Request) {
		if loader.Ready() {
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write([]byte("ok\n"))
			return
		}
		w.WriteHeader(http.StatusServiceUnavailable)
		_, _ = w.Write([]byte("api key not loaded — Secret may not exist yet\n"))
	})

	httpSrv := &http.Server{
		Addr:              *healthAddr,
		Handler:           mux,
		ReadHeaderTimeout: 5 * time.Second,
	}
	go func() {
		slog.Info("health server listening", "addr", *healthAddr)
		if err := httpSrv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			slog.Error("health server failed", "err", err)
		}
	}()

	<-ctx.Done()
	slog.Info("shutdown signal received, draining")

	shutdownCtx, shutdownCancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer shutdownCancel()

	stopped := make(chan struct{})
	go func() {
		grpcSrv.GracefulStop()
		close(stopped)
	}()

	select {
	case <-stopped:
	case <-shutdownCtx.Done():
		slog.Warn("grpc graceful stop timed out, forcing")
		grpcSrv.Stop()
	}

	_ = httpSrv.Shutdown(shutdownCtx)
	slog.Info("shutdown complete")
}

func setupLogging(level string) error {
	var lvl slog.Level
	switch level {
	case "debug":
		lvl = slog.LevelDebug
	case "info":
		lvl = slog.LevelInfo
	case "warn":
		lvl = slog.LevelWarn
	case "error":
		lvl = slog.LevelError
	default:
		return fmt.Errorf("invalid log level: %q", level)
	}
	slog.SetDefault(slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: lvl})))
	return nil
}
