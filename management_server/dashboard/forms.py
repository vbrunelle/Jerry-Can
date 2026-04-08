from django import forms

from dashboard.models import SiteConfiguration


class SiteConfigurationForm(forms.ModelForm):
    class Meta:
        model = SiteConfiguration
        fields = ['inspection_interval_minutes', 'manual_inspection_enabled']
        widgets = {
            'inspection_interval_minutes': forms.NumberInput(attrs={
                'class': 'form-control',
                'min': '1',
                'placeholder': 'Ex: 5',
            }),
            'manual_inspection_enabled': forms.CheckboxInput(attrs={
                'class': 'form-check-input',
            }),
        }
        labels = {
            'inspection_interval_minutes': "Intervalle entre les inspections (minutes)",
            'manual_inspection_enabled': "Activer le mode d'inspection manuel",
        }
