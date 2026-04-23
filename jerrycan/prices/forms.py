from django import forms

from .models import Analysis


class AnalysisForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        disable_active = kwargs.pop('disable_active', False)
        super().__init__(*args, **kwargs)

        if disable_active:
            self.fields['active'].disabled = True
            self.fields['active'].help_text = (
                'This option is temporarily disabled while an import task is running.'
            )

    def clean_active(self):
        # Keep current value when field is disabled so it cannot be changed by POST.
        if self.fields['active'].disabled:
            return self.instance.active
        return self.cleaned_data.get('active')

    class Meta:
        model = Analysis
        fields = ['data_source_url', 'update_frequency', 'max_snapshots', 'active', 'run_automatically']
        widgets = {
            'data_source_url': forms.URLInput(attrs={'class': 'form-control', 'placeholder': 'https://...'}),
            'update_frequency': forms.NumberInput(attrs={'class': 'form-control', 'min': 1}),
            'max_snapshots': forms.NumberInput(attrs={'class': 'form-control', 'min': 1}),
            'active': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'run_automatically': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }
        help_texts = {
            'update_frequency': 'In minutes.',
            'max_snapshots': 'Maximum number of snapshots shown in pivot tables.',
            'active': 'Activating this analysis will deactivate any currently active one.',
            'run_automatically': 'Automatically fetch snapshots in the background at the configured frequency.',
        }
