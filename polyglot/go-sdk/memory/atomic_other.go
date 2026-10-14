//go:build (!linux && !darwin && !windows) || (!amd64 && !arm64)

package memory

const sharedAtomics = false

// Install rejects Pulsed on these targets; no non-atomic fallback is safe.
func loadState([]byte, uint64) uint64              { panic("unsupported atomic backend") }
func casState([]byte, uint64, uint64, uint64) bool { panic("unsupported atomic backend") }
func storeState([]byte, uint64, uint64)            { panic("unsupported atomic backend") }
