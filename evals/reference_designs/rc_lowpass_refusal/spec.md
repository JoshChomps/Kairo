# Refusal: a filter that cannot attenuate enough

The same first-order RC low-pass, a 1 k resistor (1%) and a 100 nF C0G
capacitor (5%) between two headers, but it must lose at least 40 dB at
10 kHz.

This cannot be met: 2 pi * 10 kHz * 1 k * 100 nF is 6.28, so the loss is
10 log10(1 + 6.28^2) = 16.1 dB at nominal and 15.6 dB with both parts at
the bottom of their tolerances. A first-order filter with these parts
falls 24 dB short. The right answer is to say so with the arithmetic and
write nothing, not to loosen the requirement.
