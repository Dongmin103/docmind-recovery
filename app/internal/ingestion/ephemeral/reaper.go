// Copyright 2026 The InfiniFlow Authors. All Rights Reserved.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

package ephemeral

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"time"
)

// ReapReport exposes counts without plaintext names or absolute paths.
type ReapReport struct {
	Examined       int
	Removed        int
	ActiveOrNewer  int
	TooRecent      int
	Invalid        int
	CleanupFailure int
}

// Reap removes abandoned workspaces older than staleAfter. It fails closed:
// an unreadable manifest, lease-store error, missing claim, active lease, or
// newer generation is never deleted.
func (m *Manager) Reap(ctx context.Context, staleAfter time.Duration, guard LeaseGuard) (ReapReport, error) {
	var report ReapReport
	if staleAfter <= 0 {
		return report, errors.New("ephemeral: positive stale duration is required")
	}
	if guard == nil {
		return report, errors.New("ephemeral: lease guard is required for reaping")
	}
	if err := rejectSymlinkAncestry(m.root); err != nil {
		return report, err
	}
	entries, err := os.ReadDir(m.root)
	if err != nil {
		return report, fmt.Errorf("ephemeral: read workspace root: %w", err)
	}
	var reapErrors []error
	for _, entry := range entries {
		if err := ctx.Err(); err != nil {
			return report, errors.Join(err, errors.Join(reapErrors...))
		}
		if entry.Type()&os.ModeSymlink != 0 || !entry.IsDir() {
			continue
		}
		report.Examined++
		dir := filepath.Join(m.root, entry.Name())
		mf, err := readManifest(dir)
		if err != nil {
			report.Invalid++
			reapErrors = append(reapErrors, errors.New("ephemeral: invalid workspace manifest"))
			continue
		}
		if m.now().Sub(mf.TouchedAt) < staleAfter {
			report.TooRecent++
			continue
		}

		claim, ok, err := guard.ClaimCleanup(ctx, mf.JobID, mf.FencingToken)
		if err != nil {
			report.ActiveOrNewer++
			reapErrors = append(reapErrors, fmt.Errorf("ephemeral: claim cleanup ownership: %w", err))
			continue
		}
		if !ok || claim == nil {
			report.ActiveOrNewer++
			continue
		}
		removeErr := m.removeWorkspace(ctx, dir, mf, OutcomeReaped)
		releaseErr := claim.Release(context.WithoutCancel(ctx))
		if removeErr != nil || releaseErr != nil {
			report.CleanupFailure++
			reapErrors = append(reapErrors, errors.Join(removeErr, releaseErr))
			continue
		}
		report.Removed++
	}
	return report, errors.Join(reapErrors...)
}

func readManifest(dir string) (manifest, error) {
	var mf manifest
	data, err := os.ReadFile(filepath.Join(dir, manifestName))
	if err != nil {
		return mf, err
	}
	if err := json.Unmarshal(data, &mf); err != nil {
		return mf, err
	}
	if mf.SchemaVersion != 1 || mf.JobID == "" || mf.FencingToken == 0 || mf.TouchedAt.IsZero() {
		return manifest{}, errors.New("invalid manifest fields")
	}
	return mf, nil
}
