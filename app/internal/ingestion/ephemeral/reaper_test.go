// Copyright 2026 The InfiniFlow Authors. All Rights Reserved.
// Licensed under the Apache License, Version 2.0.

package ephemeral

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"testing"
	"time"
)

type cleanupClaim struct{ released bool }

func (c *cleanupClaim) Release(context.Context) error {
	c.released = true
	return nil
}

type guardResult struct {
	allow bool
	err   error
	claim *cleanupClaim
}

type fakeLeaseGuard struct {
	results map[string]guardResult
	calls   []string
}

func (g *fakeLeaseGuard) ClaimCleanup(_ context.Context, jobID string, _ uint64) (CleanupClaim, bool, error) {
	g.calls = append(g.calls, jobID)
	result := g.results[jobID]
	if !result.allow || result.err != nil {
		return nil, result.allow, result.err
	}
	return result.claim, true, nil
}

func TestReaperRemovesOnlyStaleClaimedWorkspaces(t *testing.T) {
	recorder := &memoryRecorder{}
	manager, err := NewManager(filepath.Join(t.TempDir(), "ephemeral"), recorder)
	if err != nil {
		t.Fatal(err)
	}
	base := time.Date(2026, 9, 21, 0, 0, 0, 0, time.UTC)
	manager.now = func() time.Time { return base }
	stale, err := manager.Create(t.Context(), JobIdentity{JobID: "stale", FencingToken: 1})
	if err != nil {
		t.Fatal(err)
	}
	active, err := manager.Create(t.Context(), JobIdentity{JobID: "active", FencingToken: 2})
	if err != nil {
		t.Fatal(err)
	}
	recent, err := manager.Create(t.Context(), JobIdentity{JobID: "recent", FencingToken: 3})
	if err != nil {
		t.Fatal(err)
	}
	manager.now = func() time.Time { return base.Add(2 * time.Hour) }
	if err := recent.Touch(t.Context()); err != nil {
		t.Fatal(err)
	}
	claim := &cleanupClaim{}
	guard := &fakeLeaseGuard{results: map[string]guardResult{
		"stale":  {allow: true, claim: claim},
		"active": {allow: false},
	}}
	report, err := manager.Reap(t.Context(), time.Hour, guard)
	if err != nil {
		t.Fatal(err)
	}
	if report.Removed != 1 || report.ActiveOrNewer != 1 || report.TooRecent != 1 {
		t.Fatalf("unexpected reaper report: %#v", report)
	}
	if !claim.released {
		t.Fatal("cleanup ownership claim was not released")
	}
	if _, err := os.Stat(stale.Dir()); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("claimed stale workspace remains: %v", err)
	}
	for _, dir := range []string{active.Dir(), recent.Dir()} {
		if _, err := os.Stat(dir); err != nil {
			t.Fatalf("protected workspace was removed: %v", err)
		}
	}
}

func TestReaperFailsClosedOnGuardAndManifestErrors(t *testing.T) {
	recorder := &memoryRecorder{}
	manager, err := NewManager(filepath.Join(t.TempDir(), "ephemeral"), recorder)
	if err != nil {
		t.Fatal(err)
	}
	base := time.Date(2026, 9, 21, 0, 0, 0, 0, time.UTC)
	manager.now = func() time.Time { return base }
	workspace, err := manager.Create(t.Context(), JobIdentity{JobID: "guard-error", FencingToken: 4})
	if err != nil {
		t.Fatal(err)
	}
	invalidDir := filepath.Join(manager.Root(), "unknown-directory")
	if err := os.Mkdir(invalidDir, 0o700); err != nil {
		t.Fatal(err)
	}
	manager.now = func() time.Time { return base.Add(2 * time.Hour) }
	guard := &fakeLeaseGuard{results: map[string]guardResult{
		"guard-error": {err: errors.New("lease store unavailable")},
	}}
	report, err := manager.Reap(t.Context(), time.Hour, guard)
	if err == nil {
		t.Fatal("reaper hid guard/manifest errors")
	}
	if report.ActiveOrNewer != 1 || report.Invalid != 1 || report.Removed != 0 {
		t.Fatalf("unexpected fail-closed report: %#v", report)
	}
	for _, dir := range []string{workspace.Dir(), invalidDir} {
		if _, err := os.Stat(dir); err != nil {
			t.Fatalf("fail-closed reaper removed %s: %v", filepath.Base(dir), err)
		}
	}
}

func TestReaperRequiresLeaseGuard(t *testing.T) {
	manager, err := NewManager(filepath.Join(t.TempDir(), "ephemeral"), &memoryRecorder{})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := manager.Reap(t.Context(), time.Minute, nil); err == nil {
		t.Fatal("reaper accepted a nil lease guard")
	}
}
