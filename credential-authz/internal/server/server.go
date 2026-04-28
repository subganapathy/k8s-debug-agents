// Package server implements the Envoy ext_authz v3 gRPC service. The Check RPC
// receives a CheckRequest describing an outbound HTTP request from a sidecar
// and returns a CheckResponse with headers to inject — specifically, the
// Anthropic API key as the x-api-key header.
//
// Envoy's ext_authz semantics:
//   - status.OK + OkResponse → request is allowed; OkResponse.Headers are added/overwritten
//   - status non-OK + DeniedResponse → request is rejected with the given HTTP status
//
// We never deny in normal operation. If the API key isn't loaded yet (Secret
// missing), we return Unavailable so the sidecar fails the request rather than
// forwarding it without a key — Envoy will surface a 503 to the caller.
package server

import (
	"context"
	"log/slog"

	corev3 "github.com/envoyproxy/go-control-plane/envoy/config/core/v3"
	authv3 "github.com/envoyproxy/go-control-plane/envoy/service/auth/v3"
	typev3 "github.com/envoyproxy/go-control-plane/envoy/type/v3"
	rpcstatus "google.golang.org/genproto/googleapis/rpc/status"
	"google.golang.org/grpc/codes"

	"github.com/subganapathy/k8s-debug-agents/credential-authz/internal/secret"
)

// keySource abstracts the loader so server tests can pass a fake.
type keySource interface {
	Get() (string, error)
}

// Server implements envoy.service.auth.v3.AuthorizationServer.
type Server struct {
	authv3.UnimplementedAuthorizationServer
	keys keySource
}

// New constructs a Server. The loader argument is the production source of
// truth (file-watched Secret). Tests inject a fake keySource.
func New(loader *secret.Loader) *Server {
	return &Server{keys: loader}
}

// newWithSource is the test constructor (unexported).
func newWithSource(src keySource) *Server {
	return &Server{keys: src}
}

// Check is the ext_authz entry point. Called by Envoy for every outbound
// request matched by the EnvoyFilter (configured in Step 3 to target
// api.anthropic.com).
func (s *Server) Check(ctx context.Context, req *authv3.CheckRequest) (*authv3.CheckResponse, error) {
	apiKey, err := s.keys.Get()
	if err != nil {
		// Secret hasn't been mounted/read yet. Fail closed — Envoy returns 503
		// to the caller. This is correct because forwarding without a key
		// would surface as an Anthropic 401, which is a confusing failure mode.
		slog.Warn("check denied: api key unavailable", "err", err)
		return &authv3.CheckResponse{
			Status: &rpcstatus.Status{
				Code:    int32(codes.Unavailable),
				Message: "credential-authz: api key not loaded",
			},
			HttpResponse: &authv3.CheckResponse_DeniedResponse{
				DeniedResponse: &authv3.DeniedHttpResponse{
					Status: &typev3.HttpStatus{Code: typev3.StatusCode_ServiceUnavailable},
					Body:   "credential-authz: api key not loaded\n",
				},
			},
		}, nil
	}

	// Allow the request and inject x-api-key. OVERWRITE_IF_EXISTS_OR_ADD means
	// we replace any existing x-api-key (e.g., the placeholder the SDK sent)
	// with the real key from the Secret.
	return &authv3.CheckResponse{
		Status: &rpcstatus.Status{Code: int32(codes.OK)},
		HttpResponse: &authv3.CheckResponse_OkResponse{
			OkResponse: &authv3.OkHttpResponse{
				Headers: []*corev3.HeaderValueOption{
					{
						Header: &corev3.HeaderValue{
							Key:   "x-api-key",
							Value: apiKey,
						},
						AppendAction: corev3.HeaderValueOption_OVERWRITE_IF_EXISTS_OR_ADD,
					},
				},
			},
		},
	}, nil
}
