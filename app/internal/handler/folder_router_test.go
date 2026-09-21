package handler

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"slices"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"

	"ragflow/internal/entity"
	"ragflow/internal/folderrouter"
	"ragflow/internal/service"
)

const (
	folderRouterDatasetID = "00000000000000000000000000000001"
	folderRouterDocA      = "10000000000000000000000000000001"
	folderRouterDocB      = "20000000000000000000000000000001"
)

type stubFolderFinder struct {
	candidates []folderrouter.Candidate
	err        error
}

func (f stubFolderFinder) Find(context.Context, string) ([]folderrouter.Candidate, error) {
	return f.candidates, f.err
}

type capturedFolderRouterSearch struct {
	request *service.SearchDatasetsRequest
}

type allowFolderRouterDataset struct {
	allowed bool
}

func (a allowFolderRouterDataset) Accessible(context.Context, string, string) bool {
	return a.allowed
}

func (s *capturedFolderRouterSearch) SearchDatasets(_ context.Context, request *service.SearchDatasetsRequest, _ string) (*service.SearchDatasetsResponse, error) {
	s.request = request
	return &service.SearchDatasetsResponse{}, nil
}

func TestFolderRouterSearchUsesCatalogDocumentScope(t *testing.T) {
	gin.SetMode(gin.TestMode)
	router := newFolderRouter(t)
	search := &capturedFolderRouterSearch{}
	handler, err := NewFolderRouterHandler(router, stubFolderFinder{candidates: []folderrouter.Candidate{{URI: "viking://resources/gmp-phase1/folder-a/.abstract.md", Score: 0.5}}}, allowFolderRouterDataset{allowed: true}, search, "user-1", "jina-reranker-v2-base-multilingual@jina@Jina", 500)
	if err != nil {
		t.Fatalf("NewFolderRouterHandler returned error: %v", err)
	}

	engine := gin.New()
	engine.POST("/search", func(c *gin.Context) {
		c.Set("user", &entity.User{ID: "user-1"})
		handler.Search(c)
	})
	response := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodPost, "/search", strings.NewReader(`{"question":"find folder A"}`))
	request.Header.Set("Content-Type", "application/json")
	engine.ServeHTTP(response, request)

	if response.Code != http.StatusOK {
		t.Fatalf("status=%d body=%s", response.Code, response.Body.String())
	}
	if search.request == nil {
		t.Fatal("SearchDatasets was not called")
	}
	if got, want := search.request.DatasetIDs, []string{folderRouterDatasetID}; !slices.Equal(got, want) {
		t.Fatalf("DatasetIDs=%v want %v", got, want)
	}
	if got, want := search.request.DocIDs, []string{folderRouterDocA}; !slices.Equal(got, want) {
		t.Fatalf("DocIDs=%v want %v", got, want)
	}
	if search.request.TraceID == "" || !strings.Contains(response.Body.String(), search.request.TraceID) {
		t.Fatalf("response trace does not match SearchDatasets trace: %q", search.request.TraceID)
	}
	if search.request.RerankID == nil || *search.request.RerankID != "jina-reranker-v2-base-multilingual@jina@Jina" || search.request.RerankCandidateLimit == nil || *search.request.RerankCandidateLimit != 500 || search.request.TopK == nil || *search.request.TopK != 500 || search.request.Size == nil || *search.request.Size != 5 {
		t.Fatalf("unexpected rerank contract: %#v", search.request)
	}
}

func TestFolderRouterSearchFallsBackToExplicitCatalogScope(t *testing.T) {
	gin.SetMode(gin.TestMode)
	router := newFolderRouter(t)
	search := &capturedFolderRouterSearch{}
	handler, err := NewFolderRouterHandler(router, stubFolderFinder{err: fmt.Errorf("%w: test outage", folderrouter.ErrUnavailable)}, allowFolderRouterDataset{allowed: true}, search, "user-1", "jina-reranker-v2-base-multilingual@jina@Jina", 500)
	if err != nil {
		t.Fatalf("NewFolderRouterHandler returned error: %v", err)
	}

	result, err := handler.route(context.Background(), "find anything")
	if err != nil {
		t.Fatalf("route returned error: %v", err)
	}
	if result.FallbackReason != openVikingUnavailable {
		t.Fatalf("FallbackReason=%q", result.FallbackReason)
	}
	if got, want := result.Scope.DocIDs, []string{folderRouterDocA, folderRouterDocB}; !slices.Equal(got, want) {
		t.Fatalf("fallback DocIDs=%v want %v", got, want)
	}
}

func TestFolderRouterDoesNotFallbackAfterCancellation(t *testing.T) {
	router := newFolderRouter(t)
	handler, err := NewFolderRouterHandler(router, stubFolderFinder{err: fmt.Errorf("%w: test outage", folderrouter.ErrUnavailable)}, allowFolderRouterDataset{allowed: true}, &capturedFolderRouterSearch{}, "user-1", "jina-reranker-v2-base-multilingual@jina@Jina", 500)
	if err != nil {
		t.Fatalf("NewFolderRouterHandler returned error: %v", err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := handler.route(ctx, "find anything"); !errors.Is(err, context.Canceled) {
		t.Fatalf("route error=%v want context.Canceled", err)
	}
}

func newFolderRouter(t *testing.T) folderrouter.Router {
	t.Helper()
	router, err := folderrouter.NewRouter(folderrouter.Catalog{
		DatasetID: folderRouterDatasetID,
		Folders: []folderrouter.Folder{
			{ID: "folder-a", DocIDs: []string{folderRouterDocA}},
			{ID: "folder-b", DocIDs: []string{folderRouterDocB}},
		},
	}, "viking://resources/gmp-phase1/")
	if err != nil {
		t.Fatalf("NewRouter returned error: %v", err)
	}
	return router
}
