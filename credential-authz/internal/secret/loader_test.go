package secret

import (
	"context"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestLoader_NotReadyBeforeLoad(t *testing.T) {
	l := NewLoader("/nonexistent/path")

	if l.Ready() {
		t.Error("Ready() should be false before any successful load")
	}

	if _, err := l.Get(); err == nil {
		t.Error("Get() should return error before any successful load")
	}
}

func TestLoader_ReloadSuccess(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "api-key")
	want := "sk-ant-test-key-12345"

	if err := os.WriteFile(path, []byte(want), 0o600); err != nil {
		t.Fatal(err)
	}

	l := NewLoader(path)
	if err := l.Reload(); err != nil {
		t.Fatalf("Reload(): %v", err)
	}

	if !l.Ready() {
		t.Error("Ready() should be true after successful reload")
	}

	got, err := l.Get()
	if err != nil {
		t.Fatalf("Get(): %v", err)
	}
	if got != want {
		t.Errorf("Get() = %q, want %q", got, want)
	}
}

func TestLoader_ReloadStripsWhitespace(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "api-key")

	// kubectl create secret often round-trips through base64 → bytes; trailing
	// newlines are common. Loader must strip them.
	if err := os.WriteFile(path, []byte("sk-ant-test\n  \t"), 0o600); err != nil {
		t.Fatal(err)
	}

	l := NewLoader(path)
	if err := l.Reload(); err != nil {
		t.Fatalf("Reload(): %v", err)
	}

	got, _ := l.Get()
	if got != "sk-ant-test" {
		t.Errorf("Get() = %q, want %q (whitespace not stripped)", got, "sk-ant-test")
	}
}

func TestLoader_ReloadEmptyFileRejected(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "api-key")

	if err := os.WriteFile(path, []byte(""), 0o600); err != nil {
		t.Fatal(err)
	}

	l := NewLoader(path)
	if err := l.Reload(); err == nil {
		t.Error("Reload() should error on empty file (silent zero-value would be a bad outcome)")
	}
	if l.Ready() {
		t.Error("Ready() should remain false after empty-file reload")
	}
}

func TestLoader_ReloadMissingFile(t *testing.T) {
	l := NewLoader("/nonexistent/api-key")

	if err := l.Reload(); err == nil {
		t.Error("Reload() should error when file does not exist")
	}
	if l.Ready() {
		t.Error("Ready() should remain false")
	}
}

func TestLoader_ReloadFailureKeepsLastGoodValue(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "api-key")

	// Initial good load.
	if err := os.WriteFile(path, []byte("sk-ant-good"), 0o600); err != nil {
		t.Fatal(err)
	}
	l := NewLoader(path)
	if err := l.Reload(); err != nil {
		t.Fatal(err)
	}

	// Simulate transient failure (file deleted mid-reload).
	if err := os.Remove(path); err != nil {
		t.Fatal(err)
	}
	if err := l.Reload(); err == nil {
		t.Error("Reload() should fail when file deleted")
	}

	// Last good value is still served — service degradation, not outage.
	got, err := l.Get()
	if err != nil {
		t.Errorf("Get() should return last-good value, got error: %v", err)
	}
	if got != "sk-ant-good" {
		t.Errorf("Get() = %q, want last-good %q", got, "sk-ant-good")
	}
}

func TestLoader_WatchPicksUpFileCreation(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "api-key")
	// Note: file does NOT exist yet — simulates "Secret hasn't been created".

	l := NewLoader(path)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	watchDone := make(chan error, 1)
	go func() { watchDone <- l.Watch(ctx) }()

	// Briefly wait for the watcher to attach to the directory.
	time.Sleep(100 * time.Millisecond)

	if l.Ready() {
		t.Fatal("Ready() should be false before file exists")
	}

	// Simulate the operator creating the Secret.
	if err := os.WriteFile(path, []byte("sk-ant-after-create"), 0o600); err != nil {
		t.Fatal(err)
	}

	// Allow the fsnotify event to propagate + reload to complete.
	deadline := time.Now().Add(2 * time.Second)
	for !l.Ready() && time.Now().Before(deadline) {
		time.Sleep(20 * time.Millisecond)
	}

	if !l.Ready() {
		t.Fatal("Ready() should become true after file creation event fires")
	}

	got, _ := l.Get()
	if got != "sk-ant-after-create" {
		t.Errorf("Get() = %q, want %q", got, "sk-ant-after-create")
	}

	cancel()
	select {
	case err := <-watchDone:
		if err != nil {
			t.Errorf("Watch() returned error: %v", err)
		}
	case <-time.After(time.Second):
		t.Error("Watch() did not return after ctx cancellation")
	}
}

func TestLoader_WatchPicksUpRotation(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "api-key")

	if err := os.WriteFile(path, []byte("sk-ant-v1"), 0o600); err != nil {
		t.Fatal(err)
	}

	l := NewLoader(path)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	watchDone := make(chan error, 1)
	go func() { watchDone <- l.Watch(ctx) }()

	// Wait for initial reload.
	deadline := time.Now().Add(2 * time.Second)
	for !l.Ready() && time.Now().Before(deadline) {
		time.Sleep(20 * time.Millisecond)
	}
	if !l.Ready() {
		t.Fatal("initial reload did not happen")
	}

	// Rotate the file (simulates kubelet's atomic rename — we do the simpler
	// "write file" version which fsnotify also catches).
	if err := os.WriteFile(path, []byte("sk-ant-v2"), 0o600); err != nil {
		t.Fatal(err)
	}

	deadline = time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		got, _ := l.Get()
		if got == "sk-ant-v2" {
			cancel()
			return
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Errorf("rotation not picked up; Get() never returned new value")
}
