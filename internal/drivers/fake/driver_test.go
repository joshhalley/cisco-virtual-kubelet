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

package fake

import (
	"context"
	"testing"

	"github.com/cisco/virtual-kubelet-cisco/api/v1alpha1"
	v1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

func newDriver(t *testing.T) *FAKEDriver {
	t.Helper()
	d, err := NewAppHostingDriver(context.Background(), &v1alpha1.DeviceSpec{})
	if err != nil {
		t.Fatalf("NewAppHostingDriver: %v", err)
	}
	return d
}

func makePod(ns, name, image string) *v1.Pod {
	return &v1.Pod{
		ObjectMeta: metav1.ObjectMeta{Namespace: ns, Name: name},
		Spec: v1.PodSpec{
			Containers: []v1.Container{
				{Name: "main", Image: image},
			},
		},
	}
}

// ─────────────────────────────────────────────────────────────────────────────
// DeployPod — stores the pod and marks it Running
// ─────────────────────────────────────────────────────────────────────────────

func TestDeployPod_StoresPodAndMarksRunning(t *testing.T) {
	d := newDriver(t)
	p := makePod("default", "alpha", "img:v1")

	if err := d.DeployPod(context.Background(), p); err != nil {
		t.Fatalf("DeployPod: %v", err)
	}

	got, err := d.GetPodStatus(context.Background(), p)
	if err != nil {
		t.Fatalf("GetPodStatus: %v", err)
	}
	if got.Status.Phase != v1.PodRunning {
		t.Errorf("phase = %v, want Running", got.Status.Phase)
	}
	if len(got.Status.ContainerStatuses) != 1 {
		t.Fatalf("container statuses = %d, want 1", len(got.Status.ContainerStatuses))
	}
	if got.Status.ContainerStatuses[0].Image != "img:v1" {
		t.Errorf("image = %q, want img:v1", got.Status.ContainerStatuses[0].Image)
	}
}

func TestDeployPod_MultiContainer(t *testing.T) {
	d := newDriver(t)
	p := &v1.Pod{
		ObjectMeta: metav1.ObjectMeta{Namespace: "default", Name: "multi"},
		Spec: v1.PodSpec{Containers: []v1.Container{
			{Name: "a", Image: "img-a:v1"},
			{Name: "b", Image: "img-b:v1"},
		}},
	}
	if err := d.DeployPod(context.Background(), p); err != nil {
		t.Fatalf("DeployPod: %v", err)
	}
	got, _ := d.GetPodStatus(context.Background(), p)
	if len(got.Status.ContainerStatuses) != 2 {
		t.Errorf("container statuses = %d, want 2", len(got.Status.ContainerStatuses))
	}
}

// ─────────────────────────────────────────────────────────────────────────────
// UpdatePod — spec changes apply; status preserved
// ─────────────────────────────────────────────────────────────────────────────

func TestUpdatePod_ReplacesSpecPreservesStatus(t *testing.T) {
	d := newDriver(t)
	ctx := context.Background()
	p := makePod("default", "alpha", "img:v1")
	if err := d.DeployPod(ctx, p); err != nil {
		t.Fatalf("DeployPod: %v", err)
	}

	before, _ := d.GetPodStatus(ctx, p)
	beforeStart := before.Status.StartTime

	p.Spec.Containers[0].Image = "img:v2"
	if err := d.UpdatePod(ctx, p); err != nil {
		t.Fatalf("UpdatePod: %v", err)
	}

	after, _ := d.GetPodStatus(ctx, p)
	if len(after.Spec.Containers) != 1 || after.Spec.Containers[0].Image != "img:v2" {
		t.Errorf("spec not updated; image=%q", after.Spec.Containers[0].Image)
	}
	if after.Status.Phase != v1.PodRunning {
		t.Errorf("phase = %v, want Running preserved", after.Status.Phase)
	}
	if beforeStart == nil || after.Status.StartTime == nil ||
		!beforeStart.Equal(after.Status.StartTime) {
		t.Errorf("StartTime not preserved across update")
	}
}

func TestUpdatePod_UnknownPodReturnsError(t *testing.T) {
	d := newDriver(t)
	err := d.UpdatePod(context.Background(), makePod("default", "missing", "img:v1"))
	if err == nil {
		t.Fatal("UpdatePod on unknown pod returned nil, want error")
	}
}

// ─────────────────────────────────────────────────────────────────────────────
// DeletePod — removes from store; idempotent on unknown
// ─────────────────────────────────────────────────────────────────────────────

func TestDeletePod_RemovesFromStore(t *testing.T) {
	d := newDriver(t)
	ctx := context.Background()
	p := makePod("default", "alpha", "img:v1")
	if err := d.DeployPod(ctx, p); err != nil {
		t.Fatal(err)
	}
	if err := d.DeletePod(ctx, p); err != nil {
		t.Fatalf("DeletePod: %v", err)
	}
	if _, err := d.GetPodStatus(ctx, p); err == nil {
		t.Fatal("GetPodStatus after delete returned nil error, want not-found")
	}
}

func TestDeletePod_UnknownIsNoOp(t *testing.T) {
	d := newDriver(t)
	if err := d.DeletePod(context.Background(), makePod("default", "missing", "img:v1")); err != nil {
		t.Fatalf("DeletePod on unknown pod returned %v, want nil (idempotent)", err)
	}
}

// ─────────────────────────────────────────────────────────────────────────────
// ListPods — returns all stored; empty when none
// ─────────────────────────────────────────────────────────────────────────────

func TestListPods_EmptyAndAfterInserts(t *testing.T) {
	d := newDriver(t)
	ctx := context.Background()

	got, err := d.ListPods(ctx)
	if err != nil {
		t.Fatalf("ListPods empty: %v", err)
	}
	if got == nil {
		t.Fatal("ListPods returned nil; want non-nil empty slice")
	}
	if len(got) != 0 {
		t.Errorf("len = %d, want 0", len(got))
	}

	_ = d.DeployPod(ctx, makePod("default", "a", "img"))
	_ = d.DeployPod(ctx, makePod("kube-system", "b", "img"))
	got, _ = d.ListPods(ctx)
	if len(got) != 2 {
		t.Errorf("after inserts len = %d, want 2", len(got))
	}
}

// ─────────────────────────────────────────────────────────────────────────────
// Namespace isolation — same pod name in different namespaces
// ─────────────────────────────────────────────────────────────────────────────

func TestGetPodStatus_NamespaceIsolation(t *testing.T) {
	d := newDriver(t)
	ctx := context.Background()
	_ = d.DeployPod(ctx, makePod("nsA", "same", "img-a"))
	_ = d.DeployPod(ctx, makePod("nsB", "same", "img-b"))

	a, _ := d.GetPodStatus(ctx, &v1.Pod{ObjectMeta: metav1.ObjectMeta{Namespace: "nsA", Name: "same"}})
	b, _ := d.GetPodStatus(ctx, &v1.Pod{ObjectMeta: metav1.ObjectMeta{Namespace: "nsB", Name: "same"}})
	if a.Status.ContainerStatuses[0].Image != "img-a" {
		t.Errorf("nsA image mismatch: %q", a.Status.ContainerStatuses[0].Image)
	}
	if b.Status.ContainerStatuses[0].Image != "img-b" {
		t.Errorf("nsB image mismatch: %q", b.Status.ContainerStatuses[0].Image)
	}
}

// ─────────────────────────────────────────────────────────────────────────────
// Device meta — resource / info / global oper data shapes
// ─────────────────────────────────────────────────────────────────────────────

func TestGetDeviceResources_ReturnsNonZero(t *testing.T) {
	d := newDriver(t)
	r, err := d.GetDeviceResources(context.Background())
	if err != nil {
		t.Fatalf("GetDeviceResources: %v", err)
	}
	for _, k := range []v1.ResourceName{v1.ResourceCPU, v1.ResourceMemory, v1.ResourcePods} {
		if v, ok := (*r)[k]; !ok || v.IsZero() {
			t.Errorf("%s missing or zero", k)
		}
	}
}

func TestGetDeviceInfo(t *testing.T) {
	d := newDriver(t)
	info, err := d.GetDeviceInfo(context.Background())
	if err != nil {
		t.Fatalf("GetDeviceInfo: %v", err)
	}
	if info.SerialNumber == "" || info.SoftwareVersion == "" || info.ProductID == "" {
		t.Errorf("DeviceInfo has empty fields: %+v", info)
	}
}

func TestGetGlobalOperationalData(t *testing.T) {
	d := newDriver(t)
	op, err := d.GetGlobalOperationalData(context.Background())
	if err != nil {
		t.Fatalf("GetGlobalOperationalData: %v", err)
	}
	if !op.IoxEnabled {
		t.Error("IoxEnabled = false, want true")
	}
	if op.Memory.Quota == 0 || op.Storage.Quota == 0 {
		t.Error("resource quotas zero")
	}
}
