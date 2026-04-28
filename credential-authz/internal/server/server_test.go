package server

import (
	"context"
	"errors"
	"testing"

	corev3 "github.com/envoyproxy/go-control-plane/envoy/config/core/v3"
	authv3 "github.com/envoyproxy/go-control-plane/envoy/service/auth/v3"
)

// fakeKeySource is a test double for the secret loader.
type fakeKeySource struct {
	key string
	err error
}

func (f *fakeKeySource) Get() (string, error) { return f.key, f.err }

func TestCheck_AllowsAndInjectsHeader(t *testing.T) {
	src := &fakeKeySource{key: "sk-ant-test-12345"}
	srv := newWithSource(src)

	resp, err := srv.Check(context.Background(), &authv3.CheckRequest{})
	if err != nil {
		t.Fatalf("Check() returned error: %v", err)
	}

	if resp.GetStatus().GetCode() != 0 {
		t.Errorf("status code = %d, want 0 (OK)", resp.GetStatus().GetCode())
	}

	ok := resp.GetOkResponse()
	if ok == nil {
		t.Fatal("expected OkResponse, got nil")
	}

	headers := ok.GetHeaders()
	if len(headers) != 1 {
		t.Fatalf("got %d headers, want 1", len(headers))
	}

	hdr := headers[0]
	if hdr.GetHeader().GetKey() != "x-api-key" {
		t.Errorf("header key = %q, want %q", hdr.GetHeader().GetKey(), "x-api-key")
	}
	if hdr.GetHeader().GetValue() != "sk-ant-test-12345" {
		t.Errorf("header value = %q, want injected key", hdr.GetHeader().GetValue())
	}
	if hdr.GetAppendAction() != corev3.HeaderValueOption_OVERWRITE_IF_EXISTS_OR_ADD {
		t.Errorf("append action = %v, want OVERWRITE_IF_EXISTS_OR_ADD", hdr.GetAppendAction())
	}
}

func TestCheck_DeniesWhenKeyUnavailable(t *testing.T) {
	src := &fakeKeySource{err: errors.New("not loaded")}
	srv := newWithSource(src)

	resp, err := srv.Check(context.Background(), &authv3.CheckRequest{})
	if err != nil {
		t.Fatalf("Check() returned error: %v", err)
	}

	// Non-OK status indicates the request should be denied.
	if resp.GetStatus().GetCode() == 0 {
		t.Error("expected non-OK status when key unavailable, got OK")
	}

	denied := resp.GetDeniedResponse()
	if denied == nil {
		t.Fatal("expected DeniedResponse when key unavailable, got nil")
	}

	// 503 Service Unavailable is the right code — distinguishes "your fault" (4xx)
	// from "our fault, retry later" (5xx) for the calling sidecar.
	if denied.GetStatus().GetCode() != 503 {
		t.Errorf("denied status code = %d, want 503", denied.GetStatus().GetCode())
	}
}
