from django import forms

from .models import Analysis


class AnalysisForm(forms.ModelForm):
    class Meta:
        model = Analysis
        fields = ['data_source_url', 'update_frequency', 'active', 'run_automatically']
        widgets = {
            'data_source_url': forms.URLInput(attrs={'class': 'form-control', 'placeholder': 'https://...'}),
            'update_frequency': forms.NumberInput(attrs={'class': 'form-control', 'min': 1}),
            'active': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'run_automatically': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }
        help_texts = {
            'update_frequency': 'In minutes.',
            'active': 'Activating this analysis will deactivate any currently active one.',
            'run_automatically': 'Automatically fetch snapshots in the background at the configured frequency.',
        }
