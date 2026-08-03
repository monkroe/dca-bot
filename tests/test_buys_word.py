"""Lithuanian plural for `pirkimas`, on the fill notification.

WHY THIS IS TESTED AND THE REST OF THE MESSAGE IS NOT. Everything else on that
line is a number formatted with `:.2f`. This is the one part with a RULE, the
rule has three branches and two exceptions, and it is wrong in a way nobody
reports -- a bot that writes "0 pirkimas" is not broken, it is just foreign, and
Roberto would read past it rather than file it.

The count is also the point of the line. "$1.66" alone does not say the next buy
fails; "$1.66 (0 pirkimų)" does. So the word carries meaning, not decoration.

Rule (standard Lithuanian, same shape as the `kelionė` helper in benas-bot):
  ends in 1, except 11        -> pirkimas   (vienaskaita)
  ends in 2-9, except 12-19   -> pirkimai
  everything else, incl. 0    -> pirkimų
"""
import sys

from _harness import Runner, kr


def test_singular(t):
    # The only case that reads as one purchase.
    t.check("1", kr._buys_word(1), "pirkimas")
    t.check("21", kr._buys_word(21), "pirkimas")
    t.check("101", kr._buys_word(101), "pirkimas")


def test_eleven_is_not_singular(t):
    # 11 ends in 1 and is NOT singular. This is the exception a naive
    # `n % 10 == 1` gets wrong, and 11 buys is an ordinary balance.
    t.check("11", kr._buys_word(11), "pirkimų")
    t.check("111", kr._buys_word(111), "pirkimų")


def test_plural_two_to_nine(t):
    t.check("2", kr._buys_word(2), "pirkimai")
    t.check("9", kr._buys_word(9), "pirkimai")
    t.check("34", kr._buys_word(34), "pirkimai")


def test_teens_take_the_genitive(t):
    # 12-19 end in 2-9 but are not `pirkimai`. The second exception.
    for n in (12, 13, 14, 15, 16, 17, 18, 19):
        t.check(str(n), kr._buys_word(n), "pirkimų")


def test_zero(t):
    # THE CASE THAT MATTERS. This is what prints on the morning the balance can
    # no longer cover a buy, so it is the one a reader will actually see under
    # pressure.
    t.check("0", kr._buys_word(0), "pirkimų")
    t.check("10", kr._buys_word(10), "pirkimų")
    t.check("20", kr._buys_word(20), "pirkimų")
    t.check("100", kr._buys_word(100), "pirkimų")


if __name__ == "__main__":
    sys.exit(Runner("buys word (LT plural)").run([
        ("singular", test_singular),
        ("eleven is not singular", test_eleven_is_not_singular),
        ("plural 2-9", test_plural_two_to_nine),
        ("teens take the genitive", test_teens_take_the_genitive),
        ("zero and multiples of ten", test_zero),
    ]))
