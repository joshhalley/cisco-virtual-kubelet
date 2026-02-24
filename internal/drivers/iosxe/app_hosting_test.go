package iosxe

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"testing"

	v1 "k8s.io/api/core/v1"
	corev1listers "k8s.io/client-go/listers/core/v1"
	"k8s.io/apimachinery/pkg/labels"
)

type fakeSecretNamespaceLister struct {
	secrets map[string]*v1.Secret
}

func (f *fakeSecretNamespaceLister) List(_ labels.Selector) ([]*v1.Secret, error) {
	out := make([]*v1.Secret, 0, len(f.secrets))
	for _, s := range f.secrets {
		out = append(out, s)
	}
	return out, nil
}

func (f *fakeSecretNamespaceLister) Get(name string) (*v1.Secret, error) {
	s, ok := f.secrets[name]
	if !ok {
		return nil, errors.New("not found")
	}
	return s, nil
}

var _ corev1listers.SecretNamespaceLister = (*fakeSecretNamespaceLister)(nil)

type fakeNetworkClient struct {
	postHook func(path string, payload any) error
}

func (f *fakeNetworkClient) Get(ctx context.Context, path string, result any, unmarshal func([]byte, any) error) error {
	return nil
}

func (f *fakeNetworkClient) Post(ctx context.Context, path string, payload any, marshal func(any) ([]byte, error)) error {
	if f.postHook != nil {
		return f.postHook(path, payload)
	}
	return nil
}

func (f *fakeNetworkClient) Patch(ctx context.Context, path string, payload any, marshal func(any) ([]byte, error)) error {
	return nil
}

func (f *fakeNetworkClient) Delete(ctx context.Context, path string) error { return nil }

func TestAuthFromSecret_Token(t *testing.T) {
	sec := &v1.Secret{Data: map[string][]byte{"token": []byte("abc")}}
	a, err := authFromSecret(sec)
	if err != nil {
		t.Fatalf("unexpected err: %v", err)
	}
	if a == nil || a.Token != "abc" {
		t.Fatalf("expected token auth, got %#v", a)
	}
}

func TestAuthFromSecret_DockerConfigJSON_UsernamePassword(t *testing.T) {
	cfg := map[string]any{
		"auths": map[string]any{
			"example.com": map[string]any{
				"username": "u",
				"password": "p",
			},
		},
	}
	b, _ := json.Marshal(cfg)
	sec := &v1.Secret{Data: map[string][]byte{".dockerconfigjson": b}}
	a, err := authFromSecret(sec)
	if err != nil {
		t.Fatalf("unexpected err: %v", err)
	}
	if a == nil || a.Username != "u" || a.Password != "p" {
		t.Fatalf("expected basic auth, got %#v", a)
	}
}

func TestAuthFromSecret_DockerConfigJSON_AuthField(t *testing.T) {
	auth := base64.StdEncoding.EncodeToString([]byte("u:p"))
	cfg := map[string]any{
		"auths": map[string]any{
			"example.com": map[string]any{"auth": auth},
		},
	}
	b, _ := json.Marshal(cfg)
	sec := &v1.Secret{Data: map[string][]byte{".dockerconfigjson": b}}
	a, err := authFromSecret(sec)
	if err != nil {
		t.Fatalf("unexpected err: %v", err)
	}
	if a == nil || a.Username != "u" || a.Password != "p" {
		t.Fatalf("expected decoded basic auth, got %#v", a)
	}
}

func TestAuthFromSecret_DockerConfigJSON_IdentityTokenPreferred(t *testing.T) {
	cfg := map[string]any{
		"auths": map[string]any{
			"example.com": map[string]any{"identitytoken": "tok"},
		},
	}
	b, _ := json.Marshal(cfg)
	sec := &v1.Secret{Data: map[string][]byte{".dockerconfigjson": b}}
	a, err := authFromSecret(sec)
	if err != nil {
		t.Fatalf("unexpected err: %v", err)
	}
	if a == nil || a.Token != "tok" {
		t.Fatalf("expected identity token, got %#v", a)
	}
}

func TestInstallWithRecovery_CopySuccessThenInstallDest(t *testing.T) {
	calls := []string{}
	client := &fakeNetworkClient{postHook: func(path string, payload any) error {
		calls = append(calls, path)
		if path == "/restconf/operations/Cisco-IOS-XE-rpc:app-hosting" {
			// first install fails, second succeeds
			if len(calls) == 1 {
				return errors.New("install failed")
			}
			return nil
		}
		if path == "/restconf/operations/Cisco-IOS-XE-rpc:copy" {
			return nil
		}
		return nil
	}}

	d := &XEDriver{client: client, secretLister: &fakeSecretNamespaceLister{secrets: map[string]*v1.Secret{}}}
	cfg := AppHostingConfig{AppName: "app1", ImagePath: "https://example.com/app.tar", ImagePullPolicy: "Always"}

	if err := d.installWithRecovery(context.Background(), cfg); err != nil {
		t.Fatalf("expected success, got %v", err)
	}

	// expect install, copy, install
	if len(calls) != 3 {
		t.Fatalf("expected 3 calls, got %d: %v", len(calls), calls)
	}
	if calls[0] != "/restconf/operations/Cisco-IOS-XE-rpc:app-hosting" ||
		calls[1] != "/restconf/operations/Cisco-IOS-XE-rpc:copy" ||
		calls[2] != "/restconf/operations/Cisco-IOS-XE-rpc:app-hosting" {
		t.Fatalf("unexpected call sequence: %v", calls)
	}
}

func TestInstallWithRecovery_CopyFailsThenRetryOriginalFails(t *testing.T) {
	calls := []string{}
	client := &fakeNetworkClient{postHook: func(path string, payload any) error {
		calls = append(calls, path)
		if path == "/restconf/operations/Cisco-IOS-XE-rpc:app-hosting" {
			return errors.New("install failed")
		}
		if path == "/restconf/operations/Cisco-IOS-XE-rpc:copy" {
			return errors.New("copy failed")
		}
		return nil
	}}

	d := &XEDriver{client: client, secretLister: &fakeSecretNamespaceLister{secrets: map[string]*v1.Secret{}}}
	cfg := AppHostingConfig{AppName: "app1", ImagePath: "https://example.com/app.tar", ImagePullPolicy: "Always"}

	if err := d.installWithRecovery(context.Background(), cfg); err == nil {
		t.Fatalf("expected error")
	}

	// expect install, copy, install
	if len(calls) != 3 {
		t.Fatalf("expected 3 calls, got %d: %v", len(calls), calls)
	}
}
