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

// Tests for the thin iosxe-level wrappers over the generic
// common.NetworkClient. These cover *payload shape* and *error
// wrapping* of InstallApp / ActivateApp / StartApp / StopApp /
// DeactivateApp / UninstallApp — the pieces that transform an
// intent ("install app X from package Y") into the RESTCONF RPC
// call the device expects. Actual HTTP transport is already
// covered in common/restconf_client_test.go; the mockClient below
// records args without making network calls.

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"testing"
)

const appHostingRpcPath = "/restconf/operations/Cisco-IOS-XE-rpc:app-hosting"

// mockClient implements common.NetworkClient by recording the last
// call. Only Post is used by the wrappers under test; Get/Patch/
// Delete return NotImplemented.
type mockClient struct {
	lastPath    string
	lastPayload any
	postErr     error // if non-nil, Post returns this
}

func (m *mockClient) Get(_ context.Context, _ string, _ any, _ func([]byte, any) error) error {
	return errors.New("mockClient: Get not implemented")
}
func (m *mockClient) Post(_ context.Context, path string, payload any, _ func(any) ([]byte, error)) error {
	m.lastPath = path
	m.lastPayload = payload
	return m.postErr
}
func (m *mockClient) Patch(_ context.Context, _ string, _ any, _ func(any) ([]byte, error)) error {
	return errors.New("mockClient: Patch not implemented")
}
func (m *mockClient) Delete(_ context.Context, _ string) error {
	return errors.New("mockClient: Delete not implemented")
}

func driverWith(m *mockClient) *XEDriver {
	return &XEDriver{client: m}
}

// extractOp pulls the single top-level key (the operation name like
// "install") and its inner map from the payload shape that
// appHostingRPC constructs: map[string]interface{}{op: map[string]string{...}}.
func extractOp(t *testing.T, payload any) (string, map[string]string) {
	t.Helper()
	m, ok := payload.(map[string]interface{})
	if !ok {
		t.Fatalf("payload is %T, want map[string]interface{}", payload)
	}
	if len(m) != 1 {
		t.Fatalf("payload has %d top-level keys, want exactly 1: %+v", len(m), m)
	}
	for k, v := range m {
		inner, ok := v.(map[string]string)
		if !ok {
			t.Fatalf("payload[%q] is %T, want map[string]string", k, v)
		}
		return k, inner
	}
	panic("unreachable")
}

// ─────────────────────────────────────────────────────────────────────────────
// Happy-path payload shape for each RPC wrapper
// ─────────────────────────────────────────────────────────────────────────────

func TestInstallApp_PostsInstallRpcWithPackage(t *testing.T) {
	m := &mockClient{}
	if err := driverWith(m).InstallApp(context.Background(), "myapp", "flash:/pkg.tar"); err != nil {
		t.Fatalf("InstallApp: %v", err)
	}
	if m.lastPath != appHostingRpcPath {
		t.Errorf("path = %q, want %q", m.lastPath, appHostingRpcPath)
	}
	op, args := extractOp(t, m.lastPayload)
	if op != "install" {
		t.Errorf("op = %q, want install", op)
	}
	if args["appid"] != "myapp" {
		t.Errorf("appid = %q, want myapp", args["appid"])
	}
	if args["package"] != "flash:/pkg.tar" {
		t.Errorf("package = %q, want flash:/pkg.tar", args["package"])
	}
}

func TestNoExtraArgsRpcWrappers_PostMinimalPayload(t *testing.T) {
	cases := []struct {
		name   string
		op     string
		call   func(d *XEDriver, ctx context.Context) error
		appID  string
	}{
		{"ActivateApp", "activate", func(d *XEDriver, c context.Context) error { return d.ActivateApp(c, "app1") }, "app1"},
		{"StartApp", "start", func(d *XEDriver, c context.Context) error { return d.StartApp(c, "app2") }, "app2"},
		{"StopApp", "stop", func(d *XEDriver, c context.Context) error { return d.StopApp(c, "app3") }, "app3"},
		{"DeactivateApp", "deactivate", func(d *XEDriver, c context.Context) error { return d.DeactivateApp(c, "app4") }, "app4"},
		{"UninstallApp", "uninstall", func(d *XEDriver, c context.Context) error { return d.UninstallApp(c, "app5") }, "app5"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			m := &mockClient{}
			if err := tc.call(driverWith(m), context.Background()); err != nil {
				t.Fatalf("%s: %v", tc.name, err)
			}
			if m.lastPath != appHostingRpcPath {
				t.Errorf("path = %q, want %q", m.lastPath, appHostingRpcPath)
			}
			op, args := extractOp(t, m.lastPayload)
			if op != tc.op {
				t.Errorf("op = %q, want %q", op, tc.op)
			}
			if args["appid"] != tc.appID {
				t.Errorf("appid = %q, want %q", args["appid"], tc.appID)
			}
			// Non-install RPCs must not leak extra args.
			if len(args) != 1 {
				t.Errorf("args = %+v, want {appid}", args)
			}
		})
	}
}

// ─────────────────────────────────────────────────────────────────────────────
// Error wrapping — transport errors surface with operation + app context
// ─────────────────────────────────────────────────────────────────────────────

func TestRpcWrappers_WrapTransportError(t *testing.T) {
	underlying := errors.New("boom")
	cases := []struct {
		name  string
		op    string
		call  func(d *XEDriver, ctx context.Context) error
		appID string
	}{
		{"InstallApp", "install", func(d *XEDriver, c context.Context) error { return d.InstallApp(c, "a", "flash:/p.tar") }, "a"},
		{"ActivateApp", "activate", func(d *XEDriver, c context.Context) error { return d.ActivateApp(c, "b") }, "b"},
		{"StartApp", "start", func(d *XEDriver, c context.Context) error { return d.StartApp(c, "c") }, "c"},
		{"StopApp", "stop", func(d *XEDriver, c context.Context) error { return d.StopApp(c, "d") }, "d"},
		{"DeactivateApp", "deactivate", func(d *XEDriver, c context.Context) error { return d.DeactivateApp(c, "e") }, "e"},
		{"UninstallApp", "uninstall", func(d *XEDriver, c context.Context) error { return d.UninstallApp(c, "f") }, "f"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			m := &mockClient{postErr: underlying}
			err := tc.call(driverWith(m), context.Background())
			if err == nil {
				t.Fatal("no error returned")
			}
			if !errors.Is(err, underlying) {
				t.Errorf("err does not wrap underlying (errors.Is false): %v", err)
			}
			msg := err.Error()
			if !strings.Contains(msg, tc.op) {
				t.Errorf("err %q does not mention op %q", msg, tc.op)
			}
			if !strings.Contains(msg, tc.appID) {
				t.Errorf("err %q does not mention appID %q", msg, tc.appID)
			}
		})
	}
}

// ─────────────────────────────────────────────────────────────────────────────
// Payload stability: appid field is populated even when extraParams
// is nil (regression guard — a bad refactor could drop it).
// ─────────────────────────────────────────────────────────────────────────────

func TestRpcWrappers_AppIDPopulatedWhenNoExtraParams(t *testing.T) {
	m := &mockClient{}
	for i, call := range []func() error{
		func() error { return driverWith(m).ActivateApp(context.Background(), fmt.Sprintf("app-%d", 0)) },
		func() error { return driverWith(m).StartApp(context.Background(), fmt.Sprintf("app-%d", 1)) },
		func() error { return driverWith(m).StopApp(context.Background(), fmt.Sprintf("app-%d", 2)) },
		func() error { return driverWith(m).DeactivateApp(context.Background(), fmt.Sprintf("app-%d", 3)) },
		func() error { return driverWith(m).UninstallApp(context.Background(), fmt.Sprintf("app-%d", 4)) },
	} {
		if err := call(); err != nil {
			t.Fatalf("case %d: %v", i, err)
		}
		_, args := extractOp(t, m.lastPayload)
		want := fmt.Sprintf("app-%d", i)
		if args["appid"] != want {
			t.Errorf("case %d: appid = %q, want %q", i, args["appid"], want)
		}
	}
}
