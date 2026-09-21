package folderrouter

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"
)

const maxOpenVikingResponseBytes = 1 << 20

// ErrUnavailable identifies retryable OpenViking availability failures.
var ErrUnavailable = errors.New("OpenViking unavailable")

type traceIDContextKey struct{}

// WithTraceID propagates a caller-generated trace ID to the OpenViking client.
func WithTraceID(ctx context.Context, traceID string) context.Context {
	return context.WithValue(ctx, traceIDContextKey{}, traceID)
}

func traceIDFromContext(ctx context.Context) string {
	traceID, _ := ctx.Value(traceIDContextKey{}).(string)
	return traceID
}

// HTTPFinder queries OpenViking's L0/L1 find API for folder candidates.
type HTTPFinder struct {
	endpoint  string
	apiKey    string
	targetURI string
	client    *http.Client
}

// NewHTTPFinder constructs an OpenViking finder for a fixed pilot URI root.
func NewHTTPFinder(endpoint, apiKey, targetURI string) (*HTTPFinder, error) {
	endpoint = strings.TrimRight(strings.TrimSpace(endpoint), "/")
	apiKey = strings.TrimSpace(apiKey)
	targetURI = strings.TrimSpace(targetURI)
	if endpoint == "" || apiKey == "" || targetURI == "" {
		return nil, fmt.Errorf("OpenViking endpoint, API key, and target URI are required")
	}
	return &HTTPFinder{
		endpoint:  endpoint,
		apiKey:    apiKey,
		targetURI: targetURI,
		client:    &http.Client{Timeout: 20 * time.Second},
	}, nil
}

// Find returns ordered L0/L1 resource candidates only.
func (f *HTTPFinder) Find(ctx context.Context, question string) ([]Candidate, error) {
	payload, err := json.Marshal(map[string]interface{}{
		"query":      question,
		"target_uri": f.targetURI,
		"node_limit": 2,
		"level":      "0,1",
	})
	if err != nil {
		return nil, err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, f.endpoint+"/api/v1/search/find", bytes.NewReader(payload))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-API-Key", f.apiKey)
	if traceID := traceIDFromContext(ctx); traceID != "" {
		req.Header.Set("X-Request-ID", traceID)
	}

	response, err := f.client.Do(req)
	if err != nil {
		if ctx.Err() != nil {
			return nil, ctx.Err()
		}
		return nil, fmt.Errorf("%w: %v", ErrUnavailable, err)
	}
	defer response.Body.Close()
	if response.StatusCode >= http.StatusInternalServerError {
		return nil, fmt.Errorf("%w: HTTP %d", ErrUnavailable, response.StatusCode)
	}
	if response.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("OpenViking find returned HTTP %d", response.StatusCode)
	}

	var body struct {
		Status string `json:"status"`
		Result struct {
			Resources []struct {
				URI   string  `json:"uri"`
				Score float64 `json:"score"`
			} `json:"resources"`
		} `json:"result"`
	}
	if err := json.NewDecoder(io.LimitReader(response.Body, maxOpenVikingResponseBytes)).Decode(&body); err != nil {
		return nil, err
	}
	if body.Status != "ok" {
		return nil, fmt.Errorf("OpenViking find returned status %q", body.Status)
	}
	candidates := make([]Candidate, 0, len(body.Result.Resources))
	if len(body.Result.Resources) > 2 {
		return nil, fmt.Errorf("OpenViking find returned more than two candidates")
	}
	for _, resource := range body.Result.Resources {
		candidates = append(candidates, Candidate{URI: resource.URI, Score: resource.Score})
	}
	return candidates, nil
}
