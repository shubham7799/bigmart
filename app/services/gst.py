from decimal import ROUND_HALF_UP, Decimal

# Rounding is applied at the LINE level, not once at the bill level: each line's
# subtotal/CGST/SGST/total is rounded to the nearest rupee independently, and bill
# totals are just sums of those already-rounded lines. This keeps totals
# deterministic and consistent no matter which order items are added/edited in.


def split_gst(amount: float, slab: float) -> tuple[float, float]:
    """Split the GST due on `amount` at rate `slab` (percent) into equal CGST/SGST
    halves. Assumes an intra-state sale."""
    total_gst = amount * slab / 100
    half = total_gst / 2
    return half, half


def round_line(amount: float) -> float:
    """Round to the nearest whole rupee using standard half-up rounding."""
    return float(Decimal(str(amount)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
