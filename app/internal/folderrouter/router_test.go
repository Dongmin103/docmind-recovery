package folderrouter

import (
	"math"
	"slices"
	"testing"
)

func TestRouterUsesOnlyKnownFolderUnderConfiguredRoot(t *testing.T) {
	router, err := NewRouter(testCatalog(), "viking://resources/gmp-phase1/")
	if err != nil {
		t.Fatalf("NewRouter returned error: %v", err)
	}

	result, err := router.Route([]Candidate{
		{URI: "viking://resources/other/quality-risk-management/"},
		{URI: "viking://resources/gmp-phase1/unknown/"},
		{URI: "viking://resources/gmp-phase1/folder-b/.abstract.md", Score: 0.5},
	})
	if err != nil {
		t.Fatalf("Route returned error: %v", err)
	}
	if result.SelectedFolder != "folder-b" || result.FallbackReason != "" {
		t.Fatalf("unexpected result: %#v", result)
	}
	if got, want := result.Scope.DocIDs, []string{docFixtureB1ID, docFixtureB2ID}; !slices.Equal(got, want) {
		t.Fatalf("DocIDs=%v want %v", got, want)
	}
}

func TestRouterUnionsTwoKnownFolderScopes(t *testing.T) {
	router, err := NewRouter(testCatalog(), "viking://resources/gmp-phase1")
	if err != nil {
		t.Fatalf("NewRouter returned error: %v", err)
	}

	result, err := router.Route([]Candidate{
		{URI: "viking://resources/gmp-phase1/folder-b/.abstract.md", Score: 0.5},
		{URI: "viking://resources/gmp-phase1/folder-c/.overview.md", Score: 0.4},
	})
	if err != nil {
		t.Fatalf("Route returned error: %v", err)
	}
	if got, want := result.SelectedFolders, []string{"folder-b", "folder-c"}; !slices.Equal(got, want) {
		t.Fatalf("SelectedFolders=%v want %v", got, want)
	}
	if got, want := result.Scope.DocIDs, []string{docFixtureB1ID, docFixtureB2ID, docFixtureC1ID, docFixtureC2ID}; !slices.Equal(got, want) {
		t.Fatalf("DocIDs=%v want %v", got, want)
	}
}

func TestRouterFallsBackWhenKnownFolderScoreIsLowOrInvalid(t *testing.T) {
	router, err := NewRouter(testCatalog(), "viking://resources/gmp-phase1")
	if err != nil {
		t.Fatalf("NewRouter returned error: %v", err)
	}

	for _, score := range []float64{0.099, -1, math.NaN(), math.Inf(1)} {
		result, err := router.Route([]Candidate{{URI: "viking://resources/gmp-phase1/folder-a/.abstract.md", Score: score}})
		if err != nil {
			t.Fatalf("Route returned error for score %v: %v", score, err)
		}
		if result.FallbackReason != fallbackLowConfidence || len(result.Scope.DocIDs) == 0 {
			t.Fatalf("score %v result=%#v", score, result)
		}
	}
}

func TestRouterRejectsL2DescendantURI(t *testing.T) {
	router, err := NewRouter(testCatalog(), "viking://resources/gmp-phase1")
	if err != nil {
		t.Fatalf("NewRouter returned error: %v", err)
	}

	result, err := router.Route([]Candidate{{URI: "viking://resources/gmp-phase1/folder-a/manifest.md", Score: 0.9}})
	if err != nil {
		t.Fatalf("Route returned error: %v", err)
	}
	if result.FallbackReason != fallbackNoKnownFolder {
		t.Fatalf("FallbackReason=%q", result.FallbackReason)
	}
}

func TestValidateResponseDocIDsRejectsMissingOrOutOfScopeEvidence(t *testing.T) {
	allowed := []string{docFixtureA1ID}
	if err := ValidateResponseDocIDs([]map[string]interface{}{{"doc_id": docFixtureA1ID}}, allowed); err != nil {
		t.Fatalf("ValidateResponseDocIDs returned error: %v", err)
	}
	for _, chunks := range [][]map[string]interface{}{
		{{"doc_id": docFixtureB1ID}},
		{{"document_id": docFixtureB1ID}},
		{{"content": "without a document ID"}},
	} {
		if err := ValidateResponseDocIDs(chunks, allowed); err == nil {
			t.Fatal("ValidateResponseDocIDs returned nil error")
		}
	}
}

func TestRouterFallsBackToTheCompleteExplicitCatalogScope(t *testing.T) {
	router, err := NewRouter(testCatalog(), "viking://resources/gmp-phase1")
	if err != nil {
		t.Fatalf("NewRouter returned error: %v", err)
	}

	result, err := router.Route([]Candidate{{URI: "viking://resources/gmp-phase1/not-a-folder/"}})
	if err != nil {
		t.Fatalf("Route returned error: %v", err)
	}
	if result.SelectedFolder != "" || result.FallbackReason != fallbackNoKnownFolder {
		t.Fatalf("unexpected result: %#v", result)
	}
	if got := result.TopCandidates; len(got) != 1 || got[0].URI != "viking://resources/gmp-phase1/not-a-folder/" {
		t.Fatalf("fallback candidates=%v", got)
	}
	want := []string{docFixtureA1ID, docFixtureA2ID, docFixtureB1ID, docFixtureB2ID, docFixtureC1ID, docFixtureC2ID}
	if got := result.Scope.DocIDs; !slices.Equal(got, want) {
		t.Fatalf("fallback DocIDs=%v want %v", got, want)
	}
	if len(result.Scope.DocIDs) == 0 {
		t.Fatal("fallback scope must never be empty")
	}
}
