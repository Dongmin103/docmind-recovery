package folderrouter

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"regexp"
)

var (
	hex32Pattern = regexp.MustCompile(`^[a-f0-9]{32}$`)
	slugPattern  = regexp.MustCompile(`^[a-z0-9]+(?:-[a-z0-9]+)*$`)
)

// Folder maps one virtual folder to the document IDs allowed for that folder.
type Folder struct {
	ID     string   `json:"id"`
	DocIDs []string `json:"doc_ids"`
}

// Catalog is a read-only virtual folder allowlist for one dataset.
type Catalog struct {
	DatasetID string   `json:"dataset_id"`
	Folders   []Folder `json:"folders"`
}

// SearchScope is the resolved dataset and document allowlist for search callers.
type SearchScope struct {
	DatasetID string
	DocIDs    []string
}

// ParseCatalog decodes and validates a strict JSON virtual-folder catalog.
func ParseCatalog(data []byte) (Catalog, error) {
	if err := rejectDuplicateJSONKeys(data); err != nil {
		return Catalog{}, err
	}

	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()

	var catalog Catalog
	if err := decoder.Decode(&catalog); err != nil {
		return Catalog{}, err
	}
	if err := decoder.Decode(&struct{}{}); err != io.EOF {
		if err == nil {
			return Catalog{}, fmt.Errorf("catalog JSON must contain exactly one value")
		}
		return Catalog{}, err
	}
	if err := catalog.Validate(); err != nil {
		return Catalog{}, err
	}
	return catalog, nil
}

// Validate checks the catalog shape before it is used to scope search.
func (c Catalog) Validate() error {
	if !isHex32ID(c.DatasetID) {
		return fmt.Errorf("dataset id must be a lowercase 32-character hexadecimal id")
	}
	if len(c.Folders) == 0 {
		return fmt.Errorf("at least one folder is required")
	}

	folderIDs := make(map[string]struct{}, len(c.Folders))
	docOwners := make(map[string]string)
	for _, folder := range c.Folders {
		folderID := folder.ID
		if !isSlugID(folderID) {
			return fmt.Errorf("folder id %q must be a canonical lowercase slug", folder.ID)
		}
		if _, ok := folderIDs[folderID]; ok {
			return fmt.Errorf("duplicate folder id %q", folderID)
		}
		folderIDs[folderID] = struct{}{}
		if len(folder.DocIDs) == 0 {
			return fmt.Errorf("folder %q must contain at least one document", folderID)
		}

		folderDocIDs := make(map[string]struct{}, len(folder.DocIDs))
		for _, docID := range folder.DocIDs {
			if !isHex32ID(docID) {
				return fmt.Errorf("folder %q contains document id %q that is not a lowercase 32-character hexadecimal id", folderID, docID)
			}
			if _, ok := folderDocIDs[docID]; ok {
				return fmt.Errorf("folder %q contains duplicate document id %q", folderID, docID)
			}
			folderDocIDs[docID] = struct{}{}
			if owner, ok := docOwners[docID]; ok {
				return fmt.Errorf("document id %q appears in both folder %q and folder %q", docID, owner, folderID)
			}
			docOwners[docID] = folderID
		}
	}
	return nil
}

// Resolve returns the deduplicated document IDs for the selected virtual folders.
func (c Catalog) Resolve(folderIDs []string) ([]string, error) {
	if err := c.Validate(); err != nil {
		return nil, err
	}
	if len(folderIDs) == 0 {
		return nil, fmt.Errorf("at least one folder must be selected")
	}

	folders := make(map[string]Folder, len(c.Folders))
	for _, folder := range c.Folders {
		folders[folder.ID] = folder
	}

	docSeen := make(map[string]struct{})
	docIDs := make([]string, 0)
	for _, selectedID := range folderIDs {
		if !isSlugID(selectedID) {
			return nil, fmt.Errorf("selected folder id %q must be a canonical lowercase slug", selectedID)
		}
		folder, ok := folders[selectedID]
		if !ok {
			return nil, fmt.Errorf("unknown folder id %q", selectedID)
		}
		for _, docID := range folder.DocIDs {
			if docID == "" {
				return nil, fmt.Errorf("folder %q contains a blank document id", selectedID)
			}
			if _, ok := docSeen[docID]; ok {
				continue
			}
			docSeen[docID] = struct{}{}
			docIDs = append(docIDs, docID)
		}
	}
	if len(docIDs) == 0 {
		return nil, fmt.Errorf("selected folders resolved to no documents")
	}
	return docIDs, nil
}

// ResolveScope returns the resolved dataset and document allowlist for selected folders.
func (c Catalog) ResolveScope(folderIDs []string) (SearchScope, error) {
	docIDs, err := c.Resolve(folderIDs)
	if err != nil {
		return SearchScope{}, err
	}
	return SearchScope{
		DatasetID: c.DatasetID,
		DocIDs:    docIDs,
	}, nil
}

// AllScope returns the complete, explicit catalog allowlist for a fail-closed fallback.
func (c Catalog) AllScope() (SearchScope, error) {
	if err := c.Validate(); err != nil {
		return SearchScope{}, err
	}

	folderIDs := make([]string, 0, len(c.Folders))
	for _, folder := range c.Folders {
		folderIDs = append(folderIDs, folder.ID)
	}
	return c.ResolveScope(folderIDs)
}

func rejectDuplicateJSONKeys(data []byte) error {
	decoder := json.NewDecoder(bytes.NewReader(data))
	if err := rejectDuplicateKeysInValue(decoder); err != nil {
		return err
	}
	if _, err := decoder.Token(); err != io.EOF {
		if err == nil {
			return fmt.Errorf("catalog JSON must contain exactly one value")
		}
		return err
	}
	return nil
}

func rejectDuplicateKeysInValue(decoder *json.Decoder) error {
	token, err := decoder.Token()
	if err != nil {
		return err
	}

	delim, ok := token.(json.Delim)
	if !ok {
		return nil
	}

	switch delim {
	case '{':
		seen := make(map[string]struct{})
		for decoder.More() {
			keyToken, err := decoder.Token()
			if err != nil {
				return err
			}
			key, ok := keyToken.(string)
			if !ok {
				return fmt.Errorf("object key must be a string")
			}
			if _, ok := seen[key]; ok {
				return fmt.Errorf("duplicate JSON object key %q", key)
			}
			seen[key] = struct{}{}
			if err := rejectDuplicateKeysInValue(decoder); err != nil {
				return err
			}
		}
		closeToken, err := decoder.Token()
		if err != nil {
			return err
		}
		if closeToken != json.Delim('}') {
			return fmt.Errorf("expected end of JSON object")
		}
	case '[':
		for decoder.More() {
			if err := rejectDuplicateKeysInValue(decoder); err != nil {
				return err
			}
		}
		closeToken, err := decoder.Token()
		if err != nil {
			return err
		}
		if closeToken != json.Delim(']') {
			return fmt.Errorf("expected end of JSON array")
		}
	}
	return nil
}

func isHex32ID(id string) bool {
	return hex32Pattern.MatchString(id)
}

func isSlugID(id string) bool {
	return slugPattern.MatchString(id)
}
