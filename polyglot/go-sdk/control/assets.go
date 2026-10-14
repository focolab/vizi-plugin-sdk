package control

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
)

var ErrAssetUndeclared = errors.New("asset absent from host asset mapping")
var ErrAssetMissing = errors.New("declared asset file is missing")

// AssetsFromEnv reads the host's verified asset-id -> absolute-path mapping.
// Runtime RequestAsset slots are separate and never inserted into this map.
func AssetsFromEnv() (map[string]string, error) {
	raw := os.Getenv("VIZI_PLUGIN_ASSETS")
	if raw == "" {
		return map[string]string{}, nil
	}
	var assets map[string]string
	if err := json.Unmarshal([]byte(raw), &assets); err != nil {
		return nil, fmt.Errorf("VIZI_PLUGIN_ASSETS must be a JSON object: %w", err)
	}
	if assets == nil {
		return nil, errors.New("VIZI_PLUGIN_ASSETS must be an object, not null")
	}
	for id, path := range assets {
		if id == "" || !filepath.IsAbs(path) {
			return nil, errors.New("asset mapping requires nonempty ids and absolute paths")
		}
	}
	return assets, nil
}
func AssetPath(assets map[string]string, id string) (string, error) {
	path, ok := assets[id]
	if !ok {
		return "", fmt.Errorf("%w: %s", ErrAssetUndeclared, id)
	}
	info, err := os.Stat(path)
	if errors.Is(err, os.ErrNotExist) {
		return "", fmt.Errorf("%w: %s", ErrAssetMissing, id)
	}
	if err != nil {
		return "", err
	}
	if !info.Mode().IsRegular() {
		return "", fmt.Errorf("asset %s is not a regular file", id)
	}
	return path, nil
}
