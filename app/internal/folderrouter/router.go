package folderrouter

import (
	"fmt"
	"math"
	"strings"
)

const (
	fallbackNoKnownFolder = "no_known_folder"
	fallbackLowConfidence = "low_confidence"
	minimumFolderScore    = 0.1
	maxSelectedFolders    = 2
)

// Candidate is one folder URI returned by OpenViking in rank order.
type Candidate struct {
	URI   string
	Score float64
}

// RouteResult contains only catalog-derived RAGFlow scope information.
type RouteResult struct {
	TopCandidates   []Candidate
	SelectedFolder  string
	SelectedFolders []string
	Scope           SearchScope
	FallbackReason  string
}

// Router resolves OpenViking folder URIs through an explicit catalog allowlist.
type Router struct {
	catalog Catalog
	rootURI string
}

// NewRouter constructs a router whose OpenViking candidates must be below rootURI.
func NewRouter(catalog Catalog, rootURI string) (Router, error) {
	if err := catalog.Validate(); err != nil {
		return Router{}, err
	}
	rootURI = strings.TrimSuffix(strings.TrimSpace(rootURI), "/")
	if !strings.HasPrefix(rootURI, "viking://resources/") {
		return Router{}, fmt.Errorf("root URI must be under viking://resources/")
	}
	return Router{catalog: catalog, rootURI: rootURI}, nil
}

// DatasetID returns the sole catalog dataset used for every routed scope.
func (r Router) DatasetID() string {
	return r.catalog.DatasetID
}

// ValidateResponseDocIDs rejects retrieval evidence that falls outside an explicit scope.
func ValidateResponseDocIDs(chunks []map[string]interface{}, docIDs []string) error {
	allowed := make(map[string]struct{}, len(docIDs))
	for _, docID := range docIDs {
		allowed[docID] = struct{}{}
	}
	for _, chunk := range chunks {
		docID, _ := chunk["doc_id"].(string)
		if docID == "" {
			docID, _ = chunk["document_id"].(string)
		}
		if _, ok := allowed[docID]; !ok {
			return fmt.Errorf("RAGFlow returned a chunk outside the router document scope")
		}
	}
	return nil
}

// Route accepts only known folder URIs. Unknown, malformed, and empty results fall
// back to the catalog's complete explicit allowlist rather than an unscoped search.
func (r Router) Route(candidates []Candidate) (RouteResult, error) {
	result := RouteResult{TopCandidates: append([]Candidate(nil), candidates...)}
	selectedFolders := make([]string, 0, maxSelectedFolders)
	selectedSet := make(map[string]struct{}, maxSelectedFolders)
	for _, candidate := range candidates {
		folderID, ok := r.folderID(candidate.URI)
		if !ok {
			continue
		}
		if math.IsNaN(candidate.Score) || math.IsInf(candidate.Score, 0) || candidate.Score < minimumFolderScore {
			if len(selectedFolders) > 0 {
				break
			}
			fallback, err := r.Fallback(fallbackLowConfidence)
			if err != nil {
				return RouteResult{}, err
			}
			fallback.TopCandidates = result.TopCandidates
			return fallback, nil
		}
		if _, alreadySelected := selectedSet[folderID]; alreadySelected {
			continue
		}
		selectedSet[folderID] = struct{}{}
		selectedFolders = append(selectedFolders, folderID)
		if len(selectedFolders) < maxSelectedFolders {
			continue
		}
		break
	}
	if len(selectedFolders) > 0 {
		scope, err := r.catalog.ResolveScope(selectedFolders)
		if err != nil {
			return RouteResult{}, err
		}
		result.SelectedFolder = selectedFolders[0]
		result.SelectedFolders = selectedFolders
		result.Scope = scope
		return result, nil
	}

	fallback, err := r.Fallback(fallbackNoKnownFolder)
	if err != nil {
		return RouteResult{}, err
	}
	fallback.TopCandidates = result.TopCandidates
	return fallback, nil
}

// Fallback returns the complete explicit catalog allowlist for a named routing failure.
func (r Router) Fallback(reason string) (RouteResult, error) {
	if strings.TrimSpace(reason) == "" {
		return RouteResult{}, fmt.Errorf("fallback reason is required")
	}
	scope, err := r.catalog.AllScope()
	if err != nil {
		return RouteResult{}, err
	}
	return RouteResult{Scope: scope, FallbackReason: reason}, nil
}

func (r Router) folderID(uri string) (string, bool) {
	uri = strings.TrimSpace(uri)
	prefix := r.rootURI + "/"
	if !strings.HasPrefix(uri, prefix) {
		return "", false
	}
	remainder := strings.TrimPrefix(uri, prefix)
	folderID, suffix, found := strings.Cut(remainder, "/")
	if folderID == "" {
		return "", false
	}
	if found && suffix != ".abstract.md" && suffix != ".overview.md" {
		return "", false
	}
	if _, err := r.catalog.Resolve([]string{folderID}); err != nil {
		return "", false
	}
	return folderID, true
}
