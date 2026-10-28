# Refusal: a linear regulator that cannot stay cool

The same 5 V to 3.3 V AMS1117-3.3 circuit, but the 3.3 V rail must supply
900 mA, in a 40 degC ambient, with the junction held at or below 125 degC
at 90 degC/W.

This cannot be met: (5 - 3.3) V at 0.9 A is 1.53 W before the regulator's
own current, which at 90 degC/W is 138 degC of rise. The right answer is
to say so with the arithmetic and write nothing, not to loosen the limit.
