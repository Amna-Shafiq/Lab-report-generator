"""Lab details printed on reports. Edit these for your lab."""

APP_NAME = "Dr Shafiq Aqil Clinic"   # shown in the browser tab, top bar and console window

# The app locks (password needed again) after this many minutes without activity.
AUTO_LOCK_MINUTES = 15

# Printed at the bottom-left of every report page (first line bold).
SIGNATURE = [
    "Dr. M. Shafiq Aqil",
    "M.B.B.S. (Pb), F.C.P.S.",
    "Consultant Gastroenterologist",
]

# "Printed by" dropdown shown before printing. The chosen person's name and title are
# printed at the bottom-right of every report page, beside the doctor's signature.
PRINTED_BY_CHOICES = [
    {"name": "Mehtab ur Rehman", "title": "Lab Technician"},
]

# Case form dropdowns. Values typed via "Other" are added to the list automatically
# once used. The default is pre-selected on new cases.
CONSULTANT_CHOICES = ["Dr. Shafiq Aqil"]
LOCATION_CHOICES = ["CLINIC DR SHAFIQ AQIL"]
DEFAULT_LOCATION = "CLINIC DR SHAFIQ AQIL"

# Printed under every test whose method is ICT (one line per list item).
ICT_NOTE = [
    "Sensitivity and specificity of the ICT method is less than the ELISA method.",
    "Hence results of the ICT method should not be compared with ELISA.",
    "The facility of the ELISA method is also available at this lab.",
]
