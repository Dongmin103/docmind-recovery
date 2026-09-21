package handler

import (
	"context"
	"errors"
	"fmt"
	"strings"

	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
	"go.uber.org/zap"

	"ragflow/internal/common"
	"ragflow/internal/folderrouter"
	"ragflow/internal/service"
)

const openVikingUnavailable = "openviking_unavailable"

type folderFinder interface {
	Find(context.Context, string) ([]folderrouter.Candidate, error)
}

type datasetAccessChecker interface {
	Accessible(ctx context.Context, datasetID, userID string) bool
}

// FolderRouterHandler is an opt-in pilot endpoint. It never changes generic
// dataset-search behavior and never accepts document IDs from its request body.
type FolderRouterHandler struct {
	router         folderrouter.Router
	finder         folderFinder
	accessChecker  datasetAccessChecker
	searchService  searchDatasetsService
	operatorID     string
	rerankID       string
	candidateLimit int
}

// NewFolderRouterHandler constructs the private pilot router endpoint.
func NewFolderRouterHandler(router folderrouter.Router, finder folderFinder, accessChecker datasetAccessChecker, searchService searchDatasetsService, operatorID, rerankID string, candidateLimit int) (*FolderRouterHandler, error) {
	if finder == nil {
		return nil, fmt.Errorf("folder finder is required")
	}
	if searchService == nil {
		return nil, fmt.Errorf("dataset search service is required")
	}
	if accessChecker == nil {
		return nil, fmt.Errorf("dataset access checker is required")
	}
	operatorID = strings.TrimSpace(operatorID)
	if operatorID == "" {
		return nil, fmt.Errorf("folder router operator ID is required")
	}
	rerankID = strings.TrimSpace(rerankID)
	if rerankID == "" {
		return nil, fmt.Errorf("folder router rerank ID is required")
	}
	if candidateLimit < 5 || candidateLimit > 500 {
		return nil, fmt.Errorf("folder router rerank candidate limit must be between 5 and 500")
	}
	return &FolderRouterHandler{router: router, finder: finder, accessChecker: accessChecker, searchService: searchService, operatorID: operatorID, rerankID: rerankID, candidateLimit: candidateLimit}, nil
}

type folderRouterSearchRequest struct {
	service.SearchDatasetRequest
}

type folderRouterSearchResponse struct {
	TraceID         string                          `json:"trace_id"`
	TopCandidates   []folderrouter.Candidate        `json:"top_candidates"`
	SelectedFolder  string                          `json:"selected_folder,omitempty"`
	SelectedFolders []string                        `json:"selected_folders,omitempty"`
	Scope           folderrouter.SearchScope        `json:"scope"`
	FallbackReason  string                          `json:"fallback_reason,omitempty"`
	Result          *service.SearchDatasetsResponse `json:"result"`
}

// Search searches exactly one catalog-derived document scope after folder routing.
func (h *FolderRouterHandler) Search(c *gin.Context) {
	user, errorCode, errorMessage := GetUser(c)
	if errorCode != common.CodeSuccess {
		common.ErrorWithCode(c, errorCode, errorMessage)
		return
	}
	if user.ID != h.operatorID {
		common.ResponseWithCodeData(c, common.CodeUnauthorized, nil, "not authorized for the private folder router pilot")
		return
	}

	var request folderRouterSearchRequest
	if err := c.ShouldBindJSON(&request); err != nil {
		common.ResponseWithCodeData(c, common.CodeArgumentError, nil, err.Error())
		return
	}
	request.Question = strings.TrimSpace(request.Question)
	if request.Question == "" {
		common.ResponseWithCodeData(c, common.CodeArgumentError, nil, "question is required")
		return
	}
	if len(request.DocIDs) > 0 {
		common.ResponseWithCodeData(c, common.CodeArgumentError, nil, "doc_ids are catalog-controlled for folder routing")
		return
	}
	if len(request.MetadataFilter) > 0 {
		common.ResponseWithCodeData(c, common.CodeArgumentError, nil, "meta_data_filter is not supported for folder routing")
		return
	}
	if request.SearchID != nil && strings.TrimSpace(*request.SearchID) != "" {
		common.ResponseWithCodeData(c, common.CodeArgumentError, nil, "search_id must be empty for folder routing")
		return
	}
	if err := validateSearchDatasetRequest(&request.SearchDatasetRequest); err != nil {
		common.ResponseWithCodeData(c, common.CodeArgumentError, nil, err.Error())
		return
	}
	if !h.accessChecker.Accessible(c.Request.Context(), h.router.DatasetID(), user.ID) {
		common.ResponseWithCodeData(c, common.CodeUnauthorized, nil, "not authorized for the folder router dataset")
		return
	}

	traceID := uuid.NewString()
	traceCtx := folderrouter.WithTraceID(c.Request.Context(), traceID)
	route, err := h.route(traceCtx, request.Question)
	if err != nil {
		common.ResponseWithCodeData(c, common.CodeDataError, nil, err.Error())
		return
	}
	if route.Scope.DatasetID == "" || len(route.Scope.DocIDs) == 0 {
		common.ResponseWithCodeData(c, common.CodeDataError, nil, "router resolved an empty document scope")
		return
	}
	common.Info("Folder router resolved scope", zap.String("traceID", traceID), zap.Strings("folders", route.SelectedFolders), zap.Int("docCount", len(route.Scope.DocIDs)), zap.String("fallback", route.FallbackReason))

	searchRequest := request.ToSearchDatasetsRequest(route.Scope.DatasetID)
	searchRequest.DocIDs = append([]string(nil), route.Scope.DocIDs...)
	searchRequest.RerankID = &h.rerankID
	searchRequest.TopK = &h.candidateLimit
	searchRequest.RerankCandidateLimit = &h.candidateLimit
	finalSize := 5
	searchRequest.Size = &finalSize
	searchRequest.TraceID = traceID
	result, err := h.searchService.SearchDatasets(traceCtx, searchRequest, user.ID)
	if err != nil {
		common.ResponseWithCodeData(c, common.CodeDataError, nil, err.Error())
		return
	}
	common.Info("Folder router search completed", zap.String("traceID", traceID), zap.Int64("total", result.Total))
	if err := folderrouter.ValidateResponseDocIDs(result.Chunks, route.Scope.DocIDs); err != nil {
		common.ResponseWithCodeData(c, common.CodeDataError, nil, err.Error())
		return
	}

	common.SuccessNoMessage(c, folderRouterSearchResponse{
		TraceID:         traceID,
		TopCandidates:   route.TopCandidates,
		SelectedFolder:  route.SelectedFolder,
		SelectedFolders: route.SelectedFolders,
		Scope:           route.Scope,
		FallbackReason:  route.FallbackReason,
		Result:          result,
	})
}

func (h *FolderRouterHandler) route(ctx context.Context, question string) (folderrouter.RouteResult, error) {
	candidates, err := h.finder.Find(ctx, question)
	if err != nil {
		if ctx.Err() != nil {
			return folderrouter.RouteResult{}, ctx.Err()
		}
		if errors.Is(err, folderrouter.ErrUnavailable) {
			return h.router.Fallback(openVikingUnavailable)
		}
		return folderrouter.RouteResult{}, err
	}
	return h.router.Route(candidates)
}
