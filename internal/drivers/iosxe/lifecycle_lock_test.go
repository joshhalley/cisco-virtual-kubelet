// Copyright © 2026 Cisco Systems Inc.
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

package iosxe

import (
	"context"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

func newTestDriverWithSem() *XEDriver {
	return &XEDriver{
		lifecycleSem: make(chan struct{}, 1),
	}
}

func TestAcquireLifecycleLock_ImmediateWhenFree(t *testing.T) {
	d := newTestDriverWithSem()
	ctx := context.Background()

	err := d.acquireLifecycleLock(ctx)
	if err != nil {
		t.Fatalf("Expected lock acquisition to succeed, got: %v", err)
	}

	// Verify the semaphore is held (channel has one item)
	if len(d.lifecycleSem) != 1 {
		t.Fatalf("Expected semaphore length 1, got %d", len(d.lifecycleSem))
	}

	d.releaseLifecycleLock()

	if len(d.lifecycleSem) != 0 {
		t.Fatalf("Expected semaphore length 0 after release, got %d", len(d.lifecycleSem))
	}
}

func TestAcquireLifecycleLock_BlocksWhenHeld(t *testing.T) {
	d := newTestDriverWithSem()
	ctx := context.Background()

	// Acquire the lock first
	err := d.acquireLifecycleLock(ctx)
	if err != nil {
		t.Fatalf("First lock acquisition failed: %v", err)
	}

	// Second acquire should block; use a short-lived context to prove it
	shortCtx, cancel := context.WithTimeout(ctx, 50*time.Millisecond)
	defer cancel()

	err = d.acquireLifecycleLock(shortCtx)
	if err == nil {
		t.Fatal("Expected second lock acquisition to fail due to context timeout")
	}

	// Release the first lock
	d.releaseLifecycleLock()
}

func TestAcquireLifecycleLock_RespectsContextCancellation(t *testing.T) {
	d := newTestDriverWithSem()

	// Hold the lock
	_ = d.acquireLifecycleLock(context.Background())

	// Create a context we can cancel
	ctx, cancel := context.WithCancel(context.Background())

	done := make(chan error, 1)
	go func() {
		done <- d.acquireLifecycleLock(ctx)
	}()

	// Cancel the context while the goroutine is waiting
	cancel()

	select {
	case err := <-done:
		if err == nil {
			t.Fatal("Expected error from cancelled context, got nil")
		}
	case <-time.After(1 * time.Second):
		t.Fatal("acquireLifecycleLock did not return after context cancellation")
	}

	d.releaseLifecycleLock()
}

func TestAcquireLifecycleLock_SerializesOperations(t *testing.T) {
	d := newTestDriverWithSem()
	ctx := context.Background()

	// Track the maximum concurrent holders of the lock
	var concurrent atomic.Int32
	var maxConcurrent atomic.Int32

	var wg sync.WaitGroup
	numGoroutines := 5

	for i := 0; i < numGoroutines; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()

			err := d.acquireLifecycleLock(ctx)
			if err != nil {
				t.Errorf("Lock acquisition failed: %v", err)
				return
			}

			cur := concurrent.Add(1)
			// Update max if this is a new high
			for {
				old := maxConcurrent.Load()
				if cur <= old || maxConcurrent.CompareAndSwap(old, cur) {
					break
				}
			}

			// Simulate work
			time.Sleep(10 * time.Millisecond)

			concurrent.Add(-1)
			d.releaseLifecycleLock()
		}()
	}

	wg.Wait()

	if max := maxConcurrent.Load(); max != 1 {
		t.Fatalf("Expected max concurrency of 1, got %d", max)
	}
}

func TestReleaseLifecycleLock_AllowsNextWaiter(t *testing.T) {
	d := newTestDriverWithSem()
	ctx := context.Background()

	// Acquire lock
	_ = d.acquireLifecycleLock(ctx)

	acquired := make(chan struct{})
	go func() {
		_ = d.acquireLifecycleLock(ctx)
		close(acquired)
		d.releaseLifecycleLock()
	}()

	// Verify the goroutine hasn't acquired yet
	select {
	case <-acquired:
		t.Fatal("Second acquire should not succeed while lock is held")
	case <-time.After(50 * time.Millisecond):
		// Expected: still blocked
	}

	// Release, which should unblock the waiter
	d.releaseLifecycleLock()

	select {
	case <-acquired:
		// Success
	case <-time.After(1 * time.Second):
		t.Fatal("Second acquire did not succeed after release")
	}
}
