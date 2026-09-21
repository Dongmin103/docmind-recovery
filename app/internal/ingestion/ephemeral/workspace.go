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

// Package ephemeral owns job-scoped plaintext workspaces for cloud-source
// ingestion. It deliberately has no object-storage adapter: decrypted inputs,
// thumbnails, rendered pages, OCR intermediates, and chunk images live only
// for the lifetime of one ingestion job.
package ephemeral

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"time"
)

const (
	manifestName   = ".docmind-ephemeral.json"
	inputDirName   = "input"
	derivedDirName = "derived"
)

// ArtifactKind separates the decrypted source from parser-created plaintext.
// Both kinds are removed together and neither can produce a durable URI.
type ArtifactKind string

const (
	InputArtifact   ArtifactKind = "input"
	DerivedArtifact ArtifactKind = "derived"
)

// CleanupState is persisted by the orchestration layer. COMPLETE is emitted
// only after the workspace directory has actually disappeared.
type CleanupState string

const (
	CleanupPending    CleanupState = "PENDING"
	CleanupInProgress CleanupState = "IN_PROGRESS"
	CleanupComplete   CleanupState = "COMPLETE"
	CleanupFailed     CleanupState = "CLEANUP_FAILED"
)

// Outcome explains why cleanup ran without retaining a plaintext error body.
type Outcome string

const (
	OutcomeSuccess  Outcome = "SUCCESS"
	OutcomeFailed   Outcome = "FAILED"
	OutcomeCanceled Outcome = "CANCELED"
	OutcomeTimeout  Outcome = "TIMEOUT"
	OutcomeReaped   Outcome = "REAPED"
)

// CleanupRecord contains metadata only. ErrorCode must be a stable category,
// never stdout, stderr, document text, a key, or file contents.
type CleanupRecord struct {
	JobID        string
	FencingToken uint64
	State        CleanupState
	Outcome      Outcome
	ErrorCode    string
	RecordedAt   time.Time
}

// CleanupRecorder normally persists cleanup state beside the ingestion job.
// Implementations must not treat a missing record as successful cleanup.
type CleanupRecorder interface {
	RecordCleanup(context.Context, CleanupRecord) error
}

// CleanupClaim is an exclusive, distributed cleanup ownership claim. While a
// claim is held, the job store must reject a new processing lease for the same
// job generation.
type CleanupClaim interface {
	Release(context.Context) error
}

// LeaseGuard closes the check/delete race in restart reaping. ClaimCleanup
// must atomically verify that no live processing lease owns jobID and that the
// supplied fencing token is still eligible for cleanup. ok=false is the safe
// answer for an active, newer, or otherwise uncertain generation.
type LeaseGuard interface {
	ClaimCleanup(ctx context.Context, jobID string, fencingToken uint64) (claim CleanupClaim, ok bool, err error)
}

// JobIdentity is the minimum non-content identity persisted in a workspace.
type JobIdentity struct {
	JobID        string
	FencingToken uint64
}

// Artifact is a local, job-scoped file handle descriptor. Path is intended for
// the local parser process only; it is not a stable reference and is invalid
// after cleanup.
type Artifact struct {
	Kind ArtifactKind
	Name string
	Path string
}

type manifest struct {
	SchemaVersion int       `json:"schema_version"`
	JobID         string    `json:"job_id"`
	FencingToken  uint64    `json:"fencing_token"`
	CreatedAt     time.Time `json:"created_at"`
	TouchedAt     time.Time `json:"touched_at"`
}

// Manager creates and reaps workspaces below one dedicated root. The root
// must be excluded from backups and protected by host/volume ACLs before the
// service starts; child directories and files are additionally created 0700
// and 0600 on platforms that implement POSIX modes.
type Manager struct {
	root           string
	recorder       CleanupRecorder
	cleanupTimeout time.Duration
	now            func() time.Time
	random         io.Reader
	removeAll      func(string) error
}

// NewManager validates that root is a dedicated directory rather than a
// filesystem root, creates it if needed, and returns an isolated manager.
func NewManager(root string, recorder CleanupRecorder) (*Manager, error) {
	if recorder == nil {
		return nil, errors.New("ephemeral: cleanup recorder is required")
	}
	abs, err := filepath.Abs(root)
	if err != nil {
		return nil, fmt.Errorf("ephemeral: resolve root: %w", err)
	}
	if isFilesystemRoot(abs) {
		return nil, errors.New("ephemeral: filesystem root is not an allowed workspace root")
	}
	if err := rejectSymlinkAncestry(abs); err != nil {
		return nil, err
	}
	if err := os.MkdirAll(abs, 0o700); err != nil {
		return nil, fmt.Errorf("ephemeral: create workspace root: %w", err)
	}
	if err := rejectSymlinkAncestry(abs); err != nil {
		return nil, err
	}
	if err := os.Chmod(abs, 0o700); err != nil && runtime.GOOS != "windows" {
		return nil, fmt.Errorf("ephemeral: restrict workspace root: %w", err)
	}
	return &Manager{
		root:           abs,
		recorder:       recorder,
		cleanupTimeout: 30 * time.Second,
		now:            func() time.Time { return time.Now().UTC() },
		random:         rand.Reader,
		removeAll:      os.RemoveAll,
	}, nil
}

// Root returns the dedicated ephemeral root for startup checks and metrics.
func (m *Manager) Root() string { return m.root }

// Workspace is a single job generation's plaintext boundary.
type Workspace struct {
	manager  *Manager
	dir      string
	manifest manifest
	mu       sync.Mutex
	closed   bool
}

// Create allocates a unique workspace and records cleanup as pending before
// decrypted content can be written.
func (m *Manager) Create(ctx context.Context, job JobIdentity) (*Workspace, error) {
	if err := validateJob(job); err != nil {
		return nil, err
	}

	dir, err := m.uniqueWorkspaceDir(job)
	if err != nil {
		return nil, err
	}
	if err := os.Mkdir(dir, 0o700); err != nil {
		return nil, fmt.Errorf("ephemeral: create job workspace: %w", err)
	}
	cleanupCreated := func() { _ = m.removeAll(dir) }
	for _, child := range []string{inputDirName, derivedDirName} {
		if err := os.Mkdir(filepath.Join(dir, child), 0o700); err != nil {
			cleanupCreated()
			return nil, fmt.Errorf("ephemeral: create artifact directory: %w", err)
		}
	}

	now := m.now()
	w := &Workspace{manager: m, dir: dir, manifest: manifest{
		SchemaVersion: 1,
		JobID:         job.JobID,
		FencingToken:  job.FencingToken,
		CreatedAt:     now,
		TouchedAt:     now,
	}}
	if err := w.writeManifest(); err != nil {
		cleanupCreated()
		return nil, err
	}
	if err := m.record(ctx, w.manifest, CleanupPending, "", ""); err != nil {
		cleanupCreated()
		return nil, fmt.Errorf("ephemeral: record pending cleanup: %w", err)
	}
	return w, nil
}

// Dir is the local parser boundary. It must never be returned by an API or
// persisted as a document/object reference.
func (w *Workspace) Dir() string { return w.dir }

// Identity returns the non-content job generation associated with the workspace.
func (w *Workspace) Identity() JobIdentity {
	return JobIdentity{JobID: w.manifest.JobID, FencingToken: w.manifest.FencingToken}
}

// Write stores an input or parser derivative with a restrictive file mode.
// name is one file name, not a relative path, which prevents one job from
// escaping its workspace.
func (w *Workspace) Write(ctx context.Context, kind ArtifactKind, name string, src io.Reader) (Artifact, error) {
	if err := ctx.Err(); err != nil {
		return Artifact{}, err
	}
	if src == nil {
		return Artifact{}, errors.New("ephemeral: artifact reader is required")
	}
	if err := validateArtifactName(name); err != nil {
		return Artifact{}, err
	}

	w.mu.Lock()
	defer w.mu.Unlock()
	if w.closed {
		return Artifact{}, errors.New("ephemeral: workspace is closed")
	}
	dir, err := w.artifactDir(kind)
	if err != nil {
		return Artifact{}, err
	}
	path := filepath.Join(dir, name)
	tmp, err := os.CreateTemp(dir, ".writing-*")
	if err != nil {
		return Artifact{}, fmt.Errorf("ephemeral: create artifact: %w", err)
	}
	tmpPath := tmp.Name()
	committed := false
	defer func() {
		_ = tmp.Close()
		if !committed {
			_ = os.Remove(tmpPath)
		}
	}()
	if err := tmp.Chmod(0o600); err != nil && runtime.GOOS != "windows" {
		return Artifact{}, fmt.Errorf("ephemeral: restrict artifact: %w", err)
	}
	if _, err := copyWithContext(ctx, tmp, src); err != nil {
		return Artifact{}, fmt.Errorf("ephemeral: write artifact: %w", err)
	}
	if err := tmp.Sync(); err != nil {
		return Artifact{}, fmt.Errorf("ephemeral: sync artifact: %w", err)
	}
	if err := tmp.Close(); err != nil {
		return Artifact{}, fmt.Errorf("ephemeral: close artifact: %w", err)
	}
	if err := os.Rename(tmpPath, path); err != nil {
		return Artifact{}, fmt.Errorf("ephemeral: publish local artifact: %w", err)
	}
	committed = true
	w.manifest.TouchedAt = w.manager.now()
	if err := w.writeManifest(); err != nil {
		_ = os.Remove(path)
		return Artifact{}, err
	}
	return Artifact{Kind: kind, Name: name, Path: path}, nil
}

// Touch records parser activity so the restart reaper does not consider a
// workspace old merely because a long parser stage did not create new files.
func (w *Workspace) Touch(ctx context.Context) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.closed {
		return errors.New("ephemeral: workspace is closed")
	}
	w.manifest.TouchedAt = w.manager.now()
	return w.writeManifest()
}

// Cleanup removes every plaintext input and derivative. It records failure
// instead of presenting a leftover workspace as complete.
func (w *Workspace) Cleanup(ctx context.Context, outcome Outcome) error {
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.closed {
		return nil
	}
	w.closed = true
	return w.manager.removeWorkspace(ctx, w.dir, w.manifest, outcome)
}

// Execute guarantees cleanup after success, failure, cancellation, timeout,
// and panic. Cleanup gets its own bounded context so a canceled processing
// request cannot skip deletion.
func (m *Manager) Execute(ctx context.Context, job JobIdentity, run func(context.Context, *Workspace) error) (err error) {
	if run == nil {
		return errors.New("ephemeral: run function is required")
	}
	w, err := m.Create(ctx, job)
	if err != nil {
		return err
	}
	panicking := true
	defer func() {
		if panicking {
			recovered := recover()
			cleanupCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), m.cleanupTimeout)
			_ = w.Cleanup(cleanupCtx, OutcomeFailed)
			cancel()
			panic(recovered)
		}
	}()

	err = run(ctx, w)
	panicking = false
	outcome := OutcomeFailed
	switch {
	case errors.Is(ctx.Err(), context.DeadlineExceeded):
		outcome = OutcomeTimeout
	case errors.Is(ctx.Err(), context.Canceled):
		outcome = OutcomeCanceled
	case err != nil:
		outcome = OutcomeFailed
	default:
		outcome = OutcomeSuccess
	}
	cleanupCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), m.cleanupTimeout)
	cleanupErr := w.Cleanup(cleanupCtx, outcome)
	cancel()
	return errors.Join(err, cleanupErr)
}

func (w *Workspace) artifactDir(kind ArtifactKind) (string, error) {
	switch kind {
	case InputArtifact:
		return filepath.Join(w.dir, inputDirName), nil
	case DerivedArtifact:
		return filepath.Join(w.dir, derivedDirName), nil
	default:
		return "", fmt.Errorf("ephemeral: unsupported artifact kind %q", kind)
	}
}

func (w *Workspace) writeManifest() error {
	data, err := json.Marshal(w.manifest)
	if err != nil {
		return fmt.Errorf("ephemeral: encode manifest: %w", err)
	}
	tmp, err := os.CreateTemp(w.dir, ".manifest-*")
	if err != nil {
		return fmt.Errorf("ephemeral: create manifest: %w", err)
	}
	tmpPath := tmp.Name()
	committed := false
	defer func() {
		_ = tmp.Close()
		if !committed {
			_ = os.Remove(tmpPath)
		}
	}()
	if err := tmp.Chmod(0o600); err != nil && runtime.GOOS != "windows" {
		return fmt.Errorf("ephemeral: restrict manifest: %w", err)
	}
	if _, err := tmp.Write(data); err != nil {
		return fmt.Errorf("ephemeral: write manifest: %w", err)
	}
	if err := tmp.Sync(); err != nil {
		return fmt.Errorf("ephemeral: sync manifest: %w", err)
	}
	if err := tmp.Close(); err != nil {
		return fmt.Errorf("ephemeral: close manifest: %w", err)
	}
	if err := replaceFile(tmpPath, filepath.Join(w.dir, manifestName)); err != nil {
		return fmt.Errorf("ephemeral: publish manifest: %w", err)
	}
	committed = true
	return nil
}

func (m *Manager) uniqueWorkspaceDir(job JobIdentity) (string, error) {
	randomBytes := make([]byte, 16)
	if _, err := io.ReadFull(m.random, randomBytes); err != nil {
		return "", fmt.Errorf("ephemeral: generate workspace identity: %w", err)
	}
	digest := sha256.Sum256([]byte(fmt.Sprintf("%s\x00%d\x00%s", job.JobID, job.FencingToken, hex.EncodeToString(randomBytes))))
	return filepath.Join(m.root, "job-"+hex.EncodeToString(digest[:16])), nil
}

func (m *Manager) record(ctx context.Context, mf manifest, state CleanupState, outcome Outcome, code string) error {
	return m.recorder.RecordCleanup(ctx, CleanupRecord{
		JobID:        mf.JobID,
		FencingToken: mf.FencingToken,
		State:        state,
		Outcome:      outcome,
		ErrorCode:    code,
		RecordedAt:   m.now(),
	})
}

func (m *Manager) removeWorkspace(ctx context.Context, dir string, mf manifest, outcome Outcome) error {
	startErr := m.record(ctx, mf, CleanupInProgress, outcome, "")
	removeErr := m.removeAll(dir)
	if removeErr != nil {
		recordErr := m.record(ctx, mf, CleanupFailed, outcome, "REMOVE_FAILED")
		return errors.Join(startErr, errors.New("ephemeral: workspace cleanup failed"), recordErr)
	}
	completeErr := m.record(ctx, mf, CleanupComplete, outcome, "")
	if startErr != nil || completeErr != nil {
		return errors.Join(startErr, completeErr)
	}
	return nil
}

func validateJob(job JobIdentity) error {
	if strings.TrimSpace(job.JobID) == "" {
		return errors.New("ephemeral: job ID is required")
	}
	if len(job.JobID) > 512 {
		return errors.New("ephemeral: job ID is too long")
	}
	if job.FencingToken == 0 {
		return errors.New("ephemeral: nonzero fencing token is required")
	}
	return nil
}

func validateArtifactName(name string) error {
	if name == "" || name == "." || name == ".." || filepath.Base(name) != name || strings.ContainsAny(name, "/\\\x00") {
		return errors.New("ephemeral: artifact name must be one safe file name")
	}
	return nil
}

func isFilesystemRoot(path string) bool {
	volume := filepath.VolumeName(path)
	rest := strings.TrimPrefix(path, volume)
	rest = filepath.Clean(rest)
	return rest == string(filepath.Separator) || rest == "."
}

func rejectSymlinkAncestry(path string) error {
	current := filepath.Clean(path)
	for {
		info, err := os.Lstat(current)
		switch {
		case err == nil && info.Mode()&os.ModeSymlink != 0:
			return errors.New("ephemeral: workspace root ancestry contains a symlink or junction")
		case err != nil && !errors.Is(err, os.ErrNotExist):
			return fmt.Errorf("ephemeral: inspect workspace root ancestry: %w", err)
		}
		parent := filepath.Dir(current)
		if parent == current {
			return nil
		}
		current = parent
	}
}

func copyWithContext(ctx context.Context, dst io.Writer, src io.Reader) (int64, error) {
	buf := make([]byte, 64*1024)
	var written int64
	for {
		if err := ctx.Err(); err != nil {
			return written, err
		}
		n, readErr := src.Read(buf)
		if n > 0 {
			wn, writeErr := dst.Write(buf[:n])
			written += int64(wn)
			if writeErr != nil {
				return written, writeErr
			}
			if wn != n {
				return written, io.ErrShortWrite
			}
		}
		if errors.Is(readErr, io.EOF) {
			return written, nil
		}
		if readErr != nil {
			return written, readErr
		}
	}
}

func replaceFile(source, destination string) error {
	if runtime.GOOS == "windows" {
		if err := os.Remove(destination); err != nil && !errors.Is(err, os.ErrNotExist) {
			return err
		}
	}
	return os.Rename(source, destination)
}
