// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Resolves Kubernetes capacity quantities without rounding byte allocations.
package resources

import (
	"fmt"
	"math/big"
	"strconv"
	"strings"

	"k8s.io/apimachinery/pkg/api/resource"
)

// ParsePositiveBytes resolves a capacity or memory budget to exact positive int64 bytes.
// Kubernetes owns unit meanings; the original coefficient is retained because parsing
// a whole quantity can round fractional bytes or cap oversized BinarySI values.
func ParsePositiveBytes(field, value string) (int64, error) {
	invalid := fmt.Errorf("%s must be a positive whole-byte Kubernetes quantity within int64", field)
	unitStart := strings.IndexFunc(value, func(character rune) bool {
		return !strings.ContainsRune("0123456789.+-", character)
	})
	if unitStart < 0 {
		unitStart = len(value)
	}
	number, suffix := value[:unitStart], value[unitStart:]
	scale := int64(0)
	if dot := strings.IndexByte(number, '.'); dot >= 0 {
		scale = int64(len(number) - dot - 1)
		number = number[:dot] + number[dot+1:]
	}
	coefficient, ok := new(big.Int).SetString(number, 10)
	if !ok || coefficient.Sign() <= 0 {
		return 0, invalid
	}
	if len(suffix) > 1 && (suffix[0] == 'e' || suffix[0] == 'E') && suffix != "Ei" {
		exponent, err := strconv.ParseInt(suffix[1:], 10, 64)
		// Reject exponents that cannot yield whole int64 bytes before expanding them.
		if err != nil || exponent > scale+19 || exponent < scale-int64(len(coefficient.String())) {
			return 0, invalid
		}
		scale -= exponent
	} else {
		unit, err := resource.ParseQuantity("1" + suffix)
		if err != nil {
			return 0, invalid
		}
		decimal := unit.AsDec()
		coefficient.Mul(coefficient, decimal.UnscaledBig())
		scale += int64(decimal.Scale())
	}
	if scale > 0 {
		if scale > int64(len(coefficient.String())) {
			return 0, invalid
		}
		divisor := new(big.Int).Exp(big.NewInt(10), big.NewInt(scale), nil)
		remainder := new(big.Int)
		coefficient.QuoRem(coefficient, divisor, remainder)
		if remainder.Sign() != 0 {
			return 0, invalid
		}
	} else if scale < 0 {
		if scale < -18 {
			return 0, invalid
		}
		coefficient.Mul(coefficient, new(big.Int).Exp(big.NewInt(10), big.NewInt(-scale), nil))
	}
	if !coefficient.IsInt64() || coefficient.Sign() <= 0 {
		return 0, invalid
	}
	return coefficient.Int64(), nil
}
