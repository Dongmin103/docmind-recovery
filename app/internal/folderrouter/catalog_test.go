package folderrouter

import (
	"slices"
	"testing"
)

const (
	datasetFixtureID = "00000000000000000000000000000001"
	docFixtureA1ID   = "10000000000000000000000000000001"
	docFixtureA2ID   = "10000000000000000000000000000002"
	docFixtureB1ID   = "20000000000000000000000000000001"
	docFixtureB2ID   = "20000000000000000000000000000002"
	docFixtureC1ID   = "30000000000000000000000000000001"
	docFixtureC2ID   = "30000000000000000000000000000002"
)

func TestParseCatalogDecodesValidStrictJSON(t *testing.T) {
	catalog, err := ParseCatalog([]byte(`{
		"dataset_id": "00000000000000000000000000000002",
		"folders": [
			{"id": "json-folder-a", "doc_ids": ["40000000000000000000000000000001", "40000000000000000000000000000002"]},
			{"id": "json-folder-b", "doc_ids": ["50000000000000000000000000000001"]}
		]
	}`))
	if err != nil {
		t.Fatalf("ParseCatalog returned error: %v", err)
	}

	if catalog.DatasetID != "00000000000000000000000000000002" {
		t.Fatalf("DatasetID=%q want 00000000000000000000000000000002", catalog.DatasetID)
	}
	docIDs, err := catalog.Resolve([]string{"json-folder-a", "json-folder-b"})
	if err != nil {
		t.Fatalf("Resolve returned error: %v", err)
	}
	wantDocIDs := []string{"40000000000000000000000000000001", "40000000000000000000000000000002", "50000000000000000000000000000001"}
	if !slices.Equal(docIDs, wantDocIDs) {
		t.Fatalf("docIDs=%v want %v", docIDs, wantDocIDs)
	}
}

func TestParseCatalogRejectsUnknownFields(t *testing.T) {
	_, err := ParseCatalog([]byte(`{
		"dataset_id": "00000000000000000000000000000002",
		"folders": [{"id": "json-folder", "doc_ids": ["40000000000000000000000000000001"], "extra": true}]
	}`))
	if err == nil {
		t.Fatal("ParseCatalog returned nil error")
	}
}

func TestParseCatalogRejectsTrailingJSON(t *testing.T) {
	_, err := ParseCatalog([]byte(`{
		"dataset_id": "00000000000000000000000000000002",
		"folders": [{"id": "json-folder", "doc_ids": ["40000000000000000000000000000001"]}]
	} {"dataset_id": "00000000000000000000000000000003", "folders": [{"id": "other-folder", "doc_ids": ["50000000000000000000000000000001"]}]}`))
	if err == nil {
		t.Fatal("ParseCatalog returned nil error")
	}
}

func TestParseCatalogRejectsInvalidCatalog(t *testing.T) {
	_, err := ParseCatalog([]byte(`{
		"dataset_id": "00000000000000000000000000000002",
		"folders": [{"id": "json-folder", "doc_ids": ["40000000000000000000000000000001", " "]}]
	}`))
	if err == nil {
		t.Fatal("ParseCatalog returned nil error")
	}
}

func TestParseCatalogRejectsDuplicateObjectKeys(t *testing.T) {
	tests := []struct {
		name string
		data string
	}{
		{
			name: "top-level dataset_id",
			data: `{
				"dataset_id": "00000000000000000000000000000002",
				"dataset_id": "00000000000000000000000000000003",
				"folders": [{"id": "json-folder", "doc_ids": ["40000000000000000000000000000001"]}]
			}`,
		},
		{
			name: "nested folder id",
			data: `{
				"dataset_id": "00000000000000000000000000000002",
				"folders": [{"id": "json-folder", "id": "other-folder", "doc_ids": ["40000000000000000000000000000001"]}]
			}`,
		},
		{
			name: "nested folder doc_ids",
			data: `{
				"dataset_id": "00000000000000000000000000000002",
				"folders": [{"id": "json-folder", "doc_ids": ["40000000000000000000000000000001"], "doc_ids": ["40000000000000000000000000000002"]}]
			}`,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if _, err := ParseCatalog([]byte(tt.data)); err == nil {
				t.Fatal("ParseCatalog returned nil error")
			}
		})
	}
}

func TestResolveScopeReturnsDatasetAndDocs(t *testing.T) {
	catalog := testCatalog()

	scope, err := catalog.ResolveScope([]string{"folder-b", "folder-c"})
	if err != nil {
		t.Fatalf("ResolveScope returned error: %v", err)
	}

	if scope.DatasetID != datasetFixtureID {
		t.Fatalf("DatasetID=%q want %s", scope.DatasetID, datasetFixtureID)
	}
	wantDocIDs := []string{docFixtureB1ID, docFixtureB2ID, docFixtureC1ID, docFixtureC2ID}
	if !slices.Equal(scope.DocIDs, wantDocIDs) {
		t.Fatalf("DocIDs=%v want %v", scope.DocIDs, wantDocIDs)
	}
}

func TestResolveScopeUsesExplicitCatalogAndDoesNotMutateSelection(t *testing.T) {
	catalog := Catalog{
		DatasetID: "00000000000000000000000000000004",
		Folders: []Folder{
			{ID: "explicit-folder", DocIDs: []string{"60000000000000000000000000000001", "60000000000000000000000000000002"}},
		},
	}
	selected := []string{"explicit-folder"}

	scope, err := catalog.ResolveScope(selected)
	if err != nil {
		t.Fatalf("ResolveScope returned error: %v", err)
	}

	if scope.DatasetID != "00000000000000000000000000000004" {
		t.Fatalf("DatasetID=%q want 00000000000000000000000000000004", scope.DatasetID)
	}
	if got, want := scope.DocIDs, []string{"60000000000000000000000000000001", "60000000000000000000000000000002"}; !slices.Equal(got, want) {
		t.Fatalf("DocIDs=%v want %v", got, want)
	}
	if got, want := selected, []string{"explicit-folder"}; !slices.Equal(got, want) {
		t.Fatalf("selected folders mutated to %v want %v", got, want)
	}
}

func TestValidateRejectsInvalidCatalogs(t *testing.T) {
	tests := []struct {
		name    string
		catalog Catalog
	}{
		{
			name: "blank dataset",
			catalog: Catalog{
				DatasetID: " ",
				Folders:   []Folder{{ID: "folder", DocIDs: []string{docFixtureA1ID}}},
			},
		},
		{
			name: "blank folder id",
			catalog: Catalog{
				DatasetID: datasetFixtureID,
				Folders:   []Folder{{ID: " ", DocIDs: []string{docFixtureA1ID}}},
			},
		},
		{
			name: "duplicate folder id",
			catalog: Catalog{
				DatasetID: datasetFixtureID,
				Folders: []Folder{
					{ID: "folder", DocIDs: []string{docFixtureA1ID}},
					{ID: "folder", DocIDs: []string{docFixtureA2ID}},
				},
			},
		},
		{
			name: "empty folder docs",
			catalog: Catalog{
				DatasetID: datasetFixtureID,
				Folders:   []Folder{{ID: "folder"}},
			},
		},
		{
			name: "blank doc id",
			catalog: Catalog{
				DatasetID: datasetFixtureID,
				Folders:   []Folder{{ID: "folder", DocIDs: []string{docFixtureA1ID, " "}}},
			},
		},
		{
			name: "duplicate doc in folder",
			catalog: Catalog{
				DatasetID: datasetFixtureID,
				Folders:   []Folder{{ID: "folder", DocIDs: []string{docFixtureA1ID, docFixtureA1ID}}},
			},
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if err := tt.catalog.Validate(); err == nil {
				t.Fatal("Validate returned nil error")
			}
		})
	}
}

func TestValidateRejectsMalformedIDs(t *testing.T) {
	tests := []struct {
		name    string
		catalog Catalog
	}{
		{
			name: "short dataset id",
			catalog: Catalog{
				DatasetID: "0000000000000000000000000000001",
				Folders:   []Folder{{ID: "folder", DocIDs: []string{docFixtureA1ID}}},
			},
		},
		{
			name: "uppercase dataset id",
			catalog: Catalog{
				DatasetID: "0000000000000000000000000000000A",
				Folders:   []Folder{{ID: "folder", DocIDs: []string{docFixtureA1ID}}},
			},
		},
		{
			name: "dataset id with surrounding whitespace",
			catalog: Catalog{
				DatasetID: " " + datasetFixtureID,
				Folders:   []Folder{{ID: "folder", DocIDs: []string{docFixtureA1ID}}},
			},
		},
		{
			name: "non-hex dataset id",
			catalog: Catalog{
				DatasetID: "0000000000000000000000000000000g",
				Folders:   []Folder{{ID: "folder", DocIDs: []string{docFixtureA1ID}}},
			},
		},
		{
			name: "uppercase folder id",
			catalog: Catalog{
				DatasetID: datasetFixtureID,
				Folders:   []Folder{{ID: "Folder", DocIDs: []string{docFixtureA1ID}}},
			},
		},
		{
			name: "folder id with consecutive hyphens",
			catalog: Catalog{
				DatasetID: datasetFixtureID,
				Folders:   []Folder{{ID: "bad--folder", DocIDs: []string{docFixtureA1ID}}},
			},
		},
		{
			name: "folder id with leading hyphen",
			catalog: Catalog{
				DatasetID: datasetFixtureID,
				Folders:   []Folder{{ID: "-folder", DocIDs: []string{docFixtureA1ID}}},
			},
		},
		{
			name: "folder id with underscore",
			catalog: Catalog{
				DatasetID: datasetFixtureID,
				Folders:   []Folder{{ID: "bad_folder", DocIDs: []string{docFixtureA1ID}}},
			},
		},
		{
			name: "folder id with surrounding whitespace",
			catalog: Catalog{
				DatasetID: datasetFixtureID,
				Folders:   []Folder{{ID: " folder", DocIDs: []string{docFixtureA1ID}}},
			},
		},
		{
			name: "short doc id",
			catalog: Catalog{
				DatasetID: datasetFixtureID,
				Folders:   []Folder{{ID: "folder", DocIDs: []string{"1000000000000000000000000000001"}}},
			},
		},
		{
			name: "uppercase doc id",
			catalog: Catalog{
				DatasetID: datasetFixtureID,
				Folders:   []Folder{{ID: "folder", DocIDs: []string{"1000000000000000000000000000000A"}}},
			},
		},
		{
			name: "non-hex doc id",
			catalog: Catalog{
				DatasetID: datasetFixtureID,
				Folders:   []Folder{{ID: "folder", DocIDs: []string{"1000000000000000000000000000000g"}}},
			},
		},
		{
			name: "doc id with surrounding whitespace",
			catalog: Catalog{
				DatasetID: datasetFixtureID,
				Folders:   []Folder{{ID: "folder", DocIDs: []string{docFixtureA1ID + " "}}},
			},
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if err := tt.catalog.Validate(); err == nil {
				t.Fatal("Validate returned nil error")
			}
		})
	}
}

func TestResolveRejectsUnknownFolder(t *testing.T) {
	_, err := testCatalog().Resolve([]string{"missing"})
	if err == nil {
		t.Fatal("Resolve returned nil error")
	}
}

func TestResolveRejectsMalformedSelectedFolderIDs(t *testing.T) {
	tests := []struct {
		name     string
		folderID string
	}{
		{name: "leading whitespace", folderID: " folder-b"},
		{name: "trailing whitespace", folderID: "folder-b "},
		{name: "uppercase", folderID: "Folder-B"},
		{name: "underscore", folderID: "folder_b"},
		{name: "consecutive hyphen", folderID: "folder--b"},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if _, err := testCatalog().Resolve([]string{tt.folderID}); err == nil {
				t.Fatal("Resolve returned nil error")
			}
		})
	}
}

func TestValidateRejectsDocumentInMultipleFolders(t *testing.T) {
	catalog := Catalog{
		DatasetID: datasetFixtureID,
		Folders: []Folder{
			{ID: "one", DocIDs: []string{docFixtureA1ID}},
			{ID: "two", DocIDs: []string{docFixtureA1ID}},
		},
	}

	if err := catalog.Validate(); err == nil {
		t.Fatal("Validate returned nil error")
	}
}

func TestResolveRequiresFolderSelection(t *testing.T) {
	scope, err := testCatalog().ResolveScope(nil)
	if err == nil {
		t.Fatal("ResolveScope returned nil error")
	}
	if scope.DatasetID != "" || scope.DocIDs != nil {
		t.Fatalf("ResolveScope returned scope on error: %#v", scope)
	}
}

func TestResolveDeduplicatesRepeatedSelection(t *testing.T) {
	docIDs, err := testCatalog().Resolve([]string{"folder-b", "folder-b"})
	if err != nil {
		t.Fatalf("Resolve returned error: %v", err)
	}
	want := []string{docFixtureB1ID, docFixtureB2ID}
	if !slices.Equal(docIDs, want) {
		t.Fatalf("docIDs=%v want %v", docIDs, want)
	}
}

func testCatalog() Catalog {
	return Catalog{
		DatasetID: datasetFixtureID,
		Folders: []Folder{
			{ID: "folder-a", DocIDs: []string{docFixtureA1ID, docFixtureA2ID}},
			{ID: "folder-b", DocIDs: []string{docFixtureB1ID, docFixtureB2ID}},
			{ID: "folder-c", DocIDs: []string{docFixtureC1ID, docFixtureC2ID}},
		},
	}
}
