// Copyright 2026 The InfiniFlow Authors. All Rights Reserved.
// Licensed under the Apache License, Version 2.0.

package ephemeral

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

type memoryRecorder struct {
	mu      sync.Mutex
	records []CleanupRecord
	err     error
}

func (r *memoryRecorder) RecordCleanup(_ context.Context, record CleanupRecord) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.records = append(r.records, record)
	return r.err
}

func (r *memoryRecorder) states() []CleanupState {
	r.mu.Lock()
	defer r.mu.Unlock()
	states := make([]CleanupState, 0, len(r.records))
	for _, record := range r.records {
		states = append(states, record.State)
	}
	return states
}

func TestWorkspaceContainsInputsAndDerivedArtifactsUntilCleanup(t *testing.T) {
	recorder := &memoryRecorder{}
	manager, err := NewManager(filepath.Join(t.TempDir(), "ephemeral"), recorder)
	if err != nil {
		t.Fatal(err)
	}
	workspace, err := manager.Create(t.Context(), JobIdentity{JobID: "job-1", FencingToken: 7})
	if err != nil {
		t.Fatal(err)
	}
	input, err := workspace.Write(t.Context(), InputArtifact, "source.pdf", strings.NewReader("synthetic plaintext"))
	if err != nil {
		t.Fatal(err)
	}
	derived, err := workspace.Write(t.Context(), DerivedArtifact, "page-1.png", strings.NewReader("synthetic image"))
	if err != nil {
		t.Fatal(err)
	}
	if filepath.Dir(input.Path) == filepath.Dir(derived.Path) {
		t.Fatal("input and derived artifacts must have distinct directories")
	}
	for _, artifact := range []Artifact{input, derived} {
		if _, err := os.Stat(artifact.Path); err != nil {
			t.Fatalf("artifact does not exist before cleanup: %v", err)
		}
	}
	dir := workspace.Dir()
	if err := workspace.Cleanup(t.Context(), OutcomeSuccess); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(dir); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("workspace remains after cleanup: %v", err)
	}
	wantStates := []CleanupState{CleanupPending, CleanupInProgress, CleanupComplete}
	if got := recorder.states(); !equalStates(got, wantStates) {
		t.Fatalf("cleanup states = %v, want %v", got, wantStates)
	}
}

func TestExecuteCleansEveryTerminalPath(t *testing.T) {
	tests := []struct {
		name        string
		prepare     func() (context.Context, context.CancelFunc)
		run         func(context.Context, *Workspace) error
		wantOutcome Outcome
	}{
		{
			name: "success",
			prepare: func() (context.Context, context.CancelFunc) {
				return context.WithCancel(context.Background())
			},
			run:         func(context.Context, *Workspace) error { return nil },
			wantOutcome: OutcomeSuccess,
		},
		{
			name: "failure",
			prepare: func() (context.Context, context.CancelFunc) {
				return context.WithCancel(context.Background())
			},
			run:         func(context.Context, *Workspace) error { return errors.New("synthetic failure") },
			wantOutcome: OutcomeFailed,
		},
		{
			name: "canceled",
			prepare: func() (context.Context, context.CancelFunc) {
				ctx, cancel := context.WithCancel(context.Background())
				cancel()
				return ctx, func() {}
			},
			run:         func(context.Context, *Workspace) error { return context.Canceled },
			wantOutcome: OutcomeCanceled,
		},
		{
			name: "timeout",
			prepare: func() (context.Context, context.CancelFunc) {
				ctx, cancel := context.WithDeadline(context.Background(), time.Now().Add(-time.Second))
				return ctx, cancel
			},
			run:         func(context.Context, *Workspace) error { return context.DeadlineExceeded },
			wantOutcome: OutcomeTimeout,
		},
	}
	for i, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			recorder := &memoryRecorder{}
			manager, err := NewManager(filepath.Join(t.TempDir(), "ephemeral"), recorder)
			if err != nil {
				t.Fatal(err)
			}
			ctx, cancel := test.prepare()
			defer cancel()
			var workspaceDir string
			run := func(ctx context.Context, workspace *Workspace) error {
				workspaceDir = workspace.Dir()
				if ctx.Err() == nil {
					if _, err := workspace.Write(ctx, InputArtifact, "source.txt", strings.NewReader("synthetic")); err != nil {
						return err
					}
				}
				return test.run(ctx, workspace)
			}
			executeErr := manager.Execute(ctx, JobIdentity{JobID: test.name, FencingToken: uint64(i + 1)}, run)
			if test.wantOutcome == OutcomeSuccess && executeErr != nil {
				t.Fatalf("successful Execute returned an error: %v", executeErr)
			}
			if workspaceDir != "" {
				if _, err := os.Stat(workspaceDir); !errors.Is(err, os.ErrNotExist) {
					t.Fatalf("workspace remains after %s: %v", test.name, err)
				}
			}
			recorder.mu.Lock()
			last := recorder.records[len(recorder.records)-1]
			recorder.mu.Unlock()
			if last.State != CleanupComplete || last.Outcome != test.wantOutcome {
				t.Fatalf("last cleanup record = %#v, want COMPLETE/%s", last, test.wantOutcome)
			}
		})
	}
}

func TestCleanupFailureIsNotReportedComplete(t *testing.T) {
	recorder := &memoryRecorder{}
	manager, err := NewManager(filepath.Join(t.TempDir(), "ephemeral"), recorder)
	if err != nil {
		t.Fatal(err)
	}
	workspace, err := manager.Create(t.Context(), JobIdentity{JobID: "job-cleanup-failure", FencingToken: 2})
	if err != nil {
		t.Fatal(err)
	}
	manager.removeAll = func(string) error { return errors.New("synthetic remove failure") }
	if err := workspace.Cleanup(t.Context(), OutcomeFailed); err == nil {
		t.Fatal("Cleanup returned nil after removal failure")
	}
	wantStates := []CleanupState{CleanupPending, CleanupInProgress, CleanupFailed}
	if got := recorder.states(); !equalStates(got, wantStates) {
		t.Fatalf("cleanup states = %v, want %v", got, wantStates)
	}
}

func TestExecuteCleansBeforePropagatingPanic(t *testing.T) {
	recorder := &memoryRecorder{}
	manager, err := NewManager(filepath.Join(t.TempDir(), "ephemeral"), recorder)
	if err != nil {
		t.Fatal(err)
	}
	var workspaceDir string
	func() {
		defer func() {
			if recovered := recover(); recovered == nil {
				t.Fatal("Execute did not propagate the parser panic")
			}
		}()
		_ = manager.Execute(t.Context(), JobIdentity{JobID: "panic", FencingToken: 8}, func(_ context.Context, workspace *Workspace) error {
			workspaceDir = workspace.Dir()
			if _, err := workspace.Write(t.Context(), DerivedArtifact, "partial.bin", strings.NewReader("synthetic")); err != nil {
				t.Fatal(err)
			}
			panic("synthetic parser panic")
		})
	}()
	if _, err := os.Stat(workspaceDir); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("workspace remains after panic: %v", err)
	}
	recorder.mu.Lock()
	last := recorder.records[len(recorder.records)-1]
	recorder.mu.Unlock()
	if last.State != CleanupComplete || last.Outcome != OutcomeFailed {
		t.Fatalf("last cleanup record = %#v, want COMPLETE/FAILED", last)
	}
}

func TestWriteRejectsTraversalAndClosedWorkspace(t *testing.T) {
	recorder := &memoryRecorder{}
	manager, err := NewManager(filepath.Join(t.TempDir(), "ephemeral"), recorder)
	if err != nil {
		t.Fatal(err)
	}
	workspace, err := manager.Create(t.Context(), JobIdentity{JobID: "job-path", FencingToken: 3})
	if err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"../outside", `..\\outside`, "a/b"} {
		if _, err := workspace.Write(t.Context(), InputArtifact, name, strings.NewReader("x")); err == nil {
			t.Fatalf("unsafe artifact name %q was accepted", name)
		}
	}
	if err := workspace.Cleanup(t.Context(), OutcomeCanceled); err != nil {
		t.Fatal(err)
	}
	if _, err := workspace.Write(t.Context(), InputArtifact, "late.txt", strings.NewReader("x")); err == nil {
		t.Fatal("closed workspace accepted an artifact")
	}
}

func TestNewManagerRejectsUnsafeConfiguration(t *testing.T) {
	if _, err := NewManager(t.TempDir(), nil); err == nil {
		t.Fatal("nil recorder was accepted")
	}
	root := string(filepath.Separator)
	if vol := filepath.VolumeName(t.TempDir()); vol != "" {
		root = vol + string(filepath.Separator)
	}
	if _, err := NewManager(root, &memoryRecorder{}); err == nil {
		t.Fatal("filesystem root was accepted")
	}
}

func TestNewManagerRejectsSymlinkedRootAncestry(t *testing.T) {
	realRoot := filepath.Join(t.TempDir(), "real")
	if err := os.Mkdir(realRoot, 0o700); err != nil {
		t.Fatal(err)
	}
	linkRoot := filepath.Join(t.TempDir(), "linked")
	if err := os.Symlink(realRoot, linkRoot); err != nil {
		t.Skipf("symlink creation unavailable: %v", err)
	}
	if _, err := NewManager(filepath.Join(linkRoot, "ephemeral"), &memoryRecorder{}); err == nil {
		t.Fatal("symlinked workspace ancestry was accepted")
	}
}

func equalStates(left, right []CleanupState) bool {
	if len(left) != len(right) {
		return false
	}
	for i := range left {
		if left[i] != right[i] {
			return false
		}
	}
	return true
}
