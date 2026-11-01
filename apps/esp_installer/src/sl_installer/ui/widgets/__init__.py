"""Reusable pieces of the installer screens."""

from __future__ import annotations

from .advice import AdviceCard, Notice
from .basics import (
    Badge,
    Card,
    ChoiceCard,
    Clickable,
    ElidedLabel,
    FactList,
    button,
    clear_layout,
    hline,
    set_role,
    text,
)
from .chips import StatusChip
from .credentials import CredentialList, HotspotDetails
from .fields import Collapsible, FieldBlock, PasswordInput, UpperCaseEdit
from .led import LedSwatch
from .page import BasePage
from .picture import AnimatedPicture, Picture, Spinner, StateIcon
from .steps import ProgressStepList, StepIndicator
from .topbar import TestRibbon, TopBar

__all__ = [
    "AdviceCard",
    "AnimatedPicture",
    "Badge",
    "BasePage",
    "Card",
    "ChoiceCard",
    "Clickable",
    "Collapsible",
    "CredentialList",
    "ElidedLabel",
    "FactList",
    "FieldBlock",
    "HotspotDetails",
    "LedSwatch",
    "Notice",
    "PasswordInput",
    "Picture",
    "ProgressStepList",
    "Spinner",
    "StateIcon",
    "StatusChip",
    "StepIndicator",
    "TestRibbon",
    "TopBar",
    "UpperCaseEdit",
    "button",
    "clear_layout",
    "hline",
    "set_role",
    "text",
]
