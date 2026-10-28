# Riding through a brown-out on ceramic capacitors alone

A 5 V rail within 5% comes in on a two-pin header and leaves on another.
Between them sit four 22 uF ceramic capacitors, Murata's
GRM21BR61A226ME44 (X5R, 10 V, 0805, within 20%), and nothing else. When
the input goes, the logic downstream draws 50 mA from them, and the rail
must stay above 4 V for at least 300 us.

What the capacitors give at 5 V is not what they are marked. Murata's
own measured curve for this part (its product page's DC-bias data, 0 to
10 V at 25 degC, 0.5 Vrms at 120 Hz) puts it at 44% of its 0 V value at
5.25 V, the most the rail can be. Taking that, the 20% tolerance and
X5R's 15% over temperature, each capacitor is at least 6.6 uF, and the
four 26.4 uF. From 4.75 V, the least the rail can start at, to 4 V at
50 mA that is at least 396 us: the requirement must pass.

Two things are worth knowing:

- With the marked values the same bank would seem to hold for 1.32 ms,
  more than three times as long. The same circuit asked for 500 us must
  be refused, and without the curve the check must say unverified, not
  pass: an X5R part's loss under bias is only what its maker measured.
- The curve is typical, measured on one sample, and Kairo says so in the
  result. The step across the capacitors' ESR when the supply goes is
  not counted, and the load is taken to draw the same current all the
  way down.
