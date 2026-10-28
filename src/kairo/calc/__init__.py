"""Calculations: every number Kairo reports is computed here, from stated inputs."""

from kairo.calc.capacitor import mlcc_effective_capacitance, regulator_stability
from kairo.calc.catalogue import CATALOGUE, Calculation
from kairo.calc.engine import calculate, check_limit
from kairo.calc.interval import Interval
from kairo.calc.ldo import ldo_thermal
from kairo.calc.result import Accuracy, CalcResult, Corners, Given, Missing
from kairo.calc.setting import Settable, settable
from kairo.calc.worst_case import WorstCase, judge, worst_case

__all__ = [
    "CATALOGUE",
    "Accuracy",
    "CalcResult",
    "Calculation",
    "Corners",
    "Given",
    "Interval",
    "Missing",
    "Settable",
    "WorstCase",
    "calculate",
    "check_limit",
    "judge",
    "ldo_thermal",
    "mlcc_effective_capacitance",
    "regulator_stability",
    "settable",
    "worst_case",
]
