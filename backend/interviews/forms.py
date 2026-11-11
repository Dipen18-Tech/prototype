"""Django forms: input parsing + field-level validation. Business rules stay in services.py."""
from django import forms

from .utils import CONFIG, from_ms, RUBRIC
from .services import REASONS

SCORE_CHOICES = [(i, str(i)) for i in range(1, 5)]


class MsField(forms.IntegerField):
    """Epoch-ms timestamp → aware UTC datetime."""

    def clean(self, value):
        v = super().clean(value)
        return from_ms(v) if v is not None else None


class BookConfirmForm(forms.Form):
    start = MsField()
    ack = forms.BooleanField(required=True, error_messages={"required": "Please tick the box to confirm you can attend."})


class RescheduleForm(forms.Form):
    start = MsField()


class OutcomeForm(forms.Form):
    attendance = forms.ChoiceField(choices=[("attended", "Attended"), ("no_show", "Student no-show"), ("tech_issue", "Technical issue")], initial="attended", widget=forms.RadioSelect)
    result = forms.ChoiceField(choices=[("pass", "Pass"), ("retake", "Retake later"), ("fail", "Fail")], required=False, widget=forms.RadioSelect)
    feedback = forms.CharField(required=False, max_length=600, widget=forms.Textarea(attrs={"rows": 3}))
    notes = forms.CharField(required=False, max_length=600, widget=forms.Textarea(attrs={"rows": 2}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for key, label in RUBRIC:
            self.fields[f"score_{key}"] = forms.TypedChoiceField(choices=SCORE_CHOICES, coerce=int, required=False, label=label, widget=forms.RadioSelect)

    def clean(self):
        data = super().clean()
        if data.get("attendance") == "attended":
            if not data.get("result"):
                self.add_error("result", "Choose an outcome.")
            if any(data.get(f"score_{k}") in (None, "") for k, _ in RUBRIC):
                self.add_error(None, "Score every rubric area from 1 to 4.")
            if data.get("result") in ("retake", "fail") and len((data.get("feedback") or "").strip()) < CONFIG["MIN_FEEDBACK_CHARS"]:
                self.add_error("feedback", f"Write at least {CONFIG['MIN_FEEDBACK_CHARS']} characters — the student needs to know what to work on.")
        return data

    def scores(self):
        return {k: self.cleaned_data.get(f"score_{k}") for k, _ in RUBRIC}

    def rubric_rows(self):
        return [(label, self[f"score_{k}"]) for k, label in RUBRIC]


class ReleaseForm(forms.Form):
    reason = forms.ChoiceField(choices=[(r, r) for r in REASONS], label="Reason (shared with the student)")


class AddWindowForm(forms.Form):
    day = MsField()
    start_min = forms.IntegerField(min_value=0, max_value=24 * 60)
    end_min = forms.IntegerField(min_value=0, max_value=24 * 60)


class FlagForm(forms.Form):
    journey = forms.ChoiceField(label="Student & badge")
    reason = forms.CharField(min_length=10, max_length=500, widget=forms.Textarea(attrs={"rows": 3, "placeholder": "e.g. UI Engineering L2 contest: code matched a leaked solution"}),
                             label="Reason (kept in the audit log, not shown to the student)")

    def __init__(self, *args, journeys=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["journey"].choices = journeys
