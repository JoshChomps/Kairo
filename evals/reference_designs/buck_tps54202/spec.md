# 12 V to 5 V with a TPS54202, from its datasheet

A step-down converter built round Texas Instruments' TPS54202, with every
one of the regulator's values taken from its datasheet (SLVSD26C,
February 2026), using the limits over -40 to 125 degC:

- the feedback voltage is 0.581 V to 0.611 V, which is 0.596 V within
  2.52% (page 5);
- the switching frequency is at least 390 kHz (page 5);
- the high-side current limit is at least 2.5 A (page 5);
- it is a 2 A part, 118.6 degC/W from junction to air in SOT-23-6
  (pages 1 and 4);
- the output is set by a divider from the output to the feedback pin,
  `Vout = Vref (1 + R2/R3)` (equation 3, page 10);
- a 0.1 uF capacitor goes between the BOOT and SW pins (page 11);
- the EN pin is left open to enable the part (page 3).

12 V within 5% comes in on a two-pin header, with a 10 uF ceramic on the
input. The divider is the datasheet's own for 5 V: 100 kilohms over 13.3
kilohms, each 1%. The inductor is 15 uH within 20% and saturates at
3.5 A. The output capacitor is one 47 uF 20% polymer part whose ESR is at
most 40 mohm. The 5 V rail feeds 1.5 A through a second header.

The rail must be 5 V within 7%, the inductor must never reach its
saturation current, the rail's ripple must be no more than 50 mV peak to
peak, and it must have at least 24 uF of effective capacitance.

All four can be shown from these numbers and must pass, with the
regulator's load and its current limit. Two things are worth knowing
about them:

- The datasheet's divider gives 5.08 V at nominal and up to 5.30 V over
  the tolerances, so the same circuit asked for 5 V within 5% must be
  refused.
- The regulator's stability cannot be shown: that is a matter of its
  control loop, and nothing here measures it. Kairo must say so and
  must not pass it.
