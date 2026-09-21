package folderrouter

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestHTTPFinderUsesScopedL0L1RequestAndTrace(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, request *http.Request) {
		if request.URL.Path != "/api/v1/search/find" {
			t.Fatalf("path=%q", request.URL.Path)
		}
		if request.Header.Get("X-API-Key") != "test-key" || request.Header.Get("X-Request-ID") != "trace-1" {
			t.Fatalf("unexpected headers")
		}
		var body map[string]interface{}
		if err := json.NewDecoder(request.Body).Decode(&body); err != nil {
			t.Fatalf("decode request: %v", err)
		}
		if body["target_uri"] != "viking://resources/gmp-phase1/" || body["level"] != "0,1" || body["node_limit"] != float64(2) {
			t.Fatalf("unexpected body: %#v", body)
		}
		_, _ = w.Write([]byte(`{"status":"ok","result":{"resources":[{"uri":"viking://resources/gmp-phase1/folder-a/.abstract.md","score":0.8}]}}`))
	}))
	defer server.Close()

	finder, err := NewHTTPFinder(server.URL, "test-key", "viking://resources/gmp-phase1/")
	if err != nil {
		t.Fatalf("NewHTTPFinder returned error: %v", err)
	}
	candidates, err := finder.Find(WithTraceID(context.Background(), "trace-1"), "find folder A")
	if err != nil || len(candidates) != 1 || candidates[0].Score != 0.8 {
		t.Fatalf("Find candidates=%v err=%v", candidates, err)
	}
}

func TestHTTPFinderRejectsTooManyCandidates(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(`{"status":"ok","result":{"resources":[{"uri":"a"},{"uri":"b"},{"uri":"c"}]}}`))
	}))
	defer server.Close()

	finder, err := NewHTTPFinder(server.URL, "test-key", "viking://resources/gmp-phase1/")
	if err != nil {
		t.Fatalf("NewHTTPFinder returned error: %v", err)
	}
	if _, err := finder.Find(context.Background(), "find"); err == nil {
		t.Fatal("Find returned nil error")
	}
}
