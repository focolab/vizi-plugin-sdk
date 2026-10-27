package main

import (
	"hash/crc32"
	"testing"
)

func TestCRCCheckValue(t *testing.T) {
	if got := crc32.ChecksumIEEE([]byte("123456789")); got != 0xCBF43926 {
		t.Fatalf("CRC-32 = %08x, want CBF43926", got)
	}
}
