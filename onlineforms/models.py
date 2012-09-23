from django.db import models

# choices for Form.initiator field
INITIATOR_CHOICES = [
        ('LOG', 'Logged-in SFU users'),
        ('ANY', 'Anyone, including non-SFU users'),
        # may add others if needed, e.g. instructors, admin staff, majors in a specific program, ...
        ]

# choices for the Sheet.can_view field
VIEWABLE_CHOICES = [
        ('ALL', 'Filler can see all info on previous sheets'),
        ('NON', "Filler can't see any info on other sheets (just name/email of initiator)"),
        ]

# choices for the Field.fieldtype field
FIELD_TYPE_CHOICES = [
        ('SMTX', 'Small Text (single line)'),
        ('MDTX', 'Large Text (a few lines)'),
        ('LGTX', 'Large Text (many lines)'),
        ('EMAI', 'Email address'),
        ('RADI', 'Select with radio buttons'),
        ('SEL1', 'Select with a drop-down menu'),
        ('SELN', 'Select multiple values'),
        ('LIST', 'Enter a list of short responses'),
        ('FILE', 'Upload a file'),
        ('URL', 'A web page address (URL)'),
        ]

# mapping of field types to FieldType objects that implement their logic
from onlineforms.fieldtypes import *
FIELD_TYPE_MODELS = {
        'SMTX': SmallText,
        'MDTX': MediumText,
        }
