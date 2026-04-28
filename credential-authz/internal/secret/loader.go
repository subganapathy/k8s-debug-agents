// Package secret loads the Anthropic API key from a Kubernetes Secret mounted
// as a file, and reacts to Secret rotation via fsnotify.
//
// Why a directory watcher (not a file watcher):
//
// Kubernetes mounts Secrets via a tmpfs volume managed by kubelet. When the
// Secret changes, kubelet does NOT modify the mounted file in place — it
// writes the new content to a hidden ..data directory and atomically swaps
// the symlink. Watching the file path directly misses the swap because the
// inode behind the symlink changes; watching the parent directory catches
// every event in the projected layout (CREATE, RENAME, etc.).
//
// This is the canonical pattern for K8s Secret/ConfigMap mounts in Go services.
// Used by Vault Agent Injector, Istio's pilot-agent, and many others.
package secret

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"github.com/fsnotify/fsnotify"
)

// Loader reads the API key from a file and reloads it when the file changes.
// Safe for concurrent use.
type Loader struct {
	path string

	mu     sync.RWMutex
	apiKey string
	ready  bool
	lastOK time.Time
	lastErr error
}

// NewLoader constructs a Loader for the given file path.
// The file is NOT read at construction time — call Watch (or Reload) first.
func NewLoader(path string) *Loader {
	return &Loader{path: path}
}

// Get returns the most recently loaded API key. Returns an error if no
// successful load has happened yet (Secret missing at startup).
func (l *Loader) Get() (string, error) {
	l.mu.RLock()
	defer l.mu.RUnlock()
	if !l.ready {
		if l.lastErr != nil {
			return "", fmt.Errorf("api key not yet loaded: %w", l.lastErr)
		}
		return "", errors.New("api key not yet loaded")
	}
	return l.apiKey, nil
}

// Ready reports whether at least one successful load has happened.
// Used by /readyz to keep the pod NotReady until the Secret exists.
func (l *Loader) Ready() bool {
	l.mu.RLock()
	defer l.mu.RUnlock()
	return l.ready
}

// LastReloadAt returns the timestamp of the last successful read.
// Used for diagnostics; not load-bearing for correctness.
func (l *Loader) LastReloadAt() time.Time {
	l.mu.RLock()
	defer l.mu.RUnlock()
	return l.lastOK
}

// Reload reads the file once and atomically swaps in the new value if
// successful. On failure, the previous value (if any) is retained — so a
// transient read failure during rotation doesn't tear down service.
func (l *Loader) Reload() error {
	data, err := os.ReadFile(l.path)
	if err != nil {
		l.mu.Lock()
		l.lastErr = err
		l.mu.Unlock()
		return err
	}

	key := strings.TrimSpace(string(data))
	if key == "" {
		err := errors.New("file is empty")
		l.mu.Lock()
		l.lastErr = err
		l.mu.Unlock()
		return err
	}

	l.mu.Lock()
	l.apiKey = key
	l.ready = true
	l.lastOK = time.Now()
	l.lastErr = nil
	l.mu.Unlock()
	return nil
}

// Watch attempts an initial Reload, then runs a fsnotify loop that calls
// Reload on any event in the parent directory of the configured path.
// Returns when ctx is cancelled. Returning a non-nil error indicates the
// watcher itself failed; transient Reload errors are logged but do not stop
// the loop (they leave the previous value in place).
func (l *Loader) Watch(ctx context.Context) error {
	dir := filepath.Dir(l.path)

	watcher, err := fsnotify.NewWatcher()
	if err != nil {
		return fmt.Errorf("fsnotify.NewWatcher: %w", err)
	}
	defer func() { _ = watcher.Close() }()

	// We watch the parent directory because kubelet's atomic-rename pattern
	// for Secret updates doesn't trigger inotify events on the file itself.
	if err := watcher.Add(dir); err != nil {
		// Directory doesn't exist yet — that's fine, the Secret may not be
		// created until later. Try to load once (will also fail) and start
		// a poll loop that retries adding the directory periodically.
		slog.Info("watch directory does not exist yet; will retry", "dir", dir, "err", err)
		return l.watchWithRetry(ctx, watcher, dir)
	}

	// Initial load attempt. Failure is expected if the Secret hasn't been
	// created yet — Ready() returns false, /readyz returns 503, pod stays
	// NotReady. The first directory event after Secret creation will trigger
	// a successful reload.
	if err := l.Reload(); err != nil {
		slog.Info("initial reload failed (Secret may not exist yet)", "err", err)
	} else {
		slog.Info("initial api key load succeeded")
	}

	return l.watchLoop(ctx, watcher)
}

// watchWithRetry handles the case where the watch directory doesn't exist
// at startup (e.g., Secret hasn't been mounted). We poll for the directory
// to appear, then transition to event-driven watching.
func (l *Loader) watchWithRetry(ctx context.Context, watcher *fsnotify.Watcher, dir string) error {
	ticker := time.NewTicker(2 * time.Second)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return nil
		case <-ticker.C:
			if err := watcher.Add(dir); err != nil {
				continue
			}
			slog.Info("watch directory now exists; switching to event-driven", "dir", dir)
			if err := l.Reload(); err != nil {
				slog.Warn("reload failed after directory appeared", "err", err)
			}
			return l.watchLoop(ctx, watcher)
		}
	}
}

// watchLoop is the steady-state event consumer.
func (l *Loader) watchLoop(ctx context.Context, watcher *fsnotify.Watcher) error {
	// Long-tail safety net: even if events stop firing for some reason (rare
	// kernel/fs corner cases), this forces a reload every hour. Keeps the
	// loader correct even if the watcher silently dies.
	safetyTicker := time.NewTicker(1 * time.Hour)
	defer safetyTicker.Stop()

	for {
		select {
		case <-ctx.Done():
			return nil
		case event, ok := <-watcher.Events:
			if !ok {
				return errors.New("watcher events channel closed unexpectedly")
			}
			if err := l.Reload(); err != nil {
				slog.Warn("reload failed", "trigger", event.Op.String(), "path", event.Name, "err", err)
			} else {
				slog.Info("api key reloaded", "trigger", event.Op.String(), "path", event.Name)
			}
		case err, ok := <-watcher.Errors:
			if !ok {
				return errors.New("watcher errors channel closed unexpectedly")
			}
			slog.Error("watcher error", "err", err)
		case <-safetyTicker.C:
			if err := l.Reload(); err != nil {
				slog.Warn("safety-tick reload failed", "err", err)
			}
		}
	}
}
