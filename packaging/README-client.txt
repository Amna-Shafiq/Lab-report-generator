Dr Shafiq Aqil Clinic - test version
====================================

How to start
1. Unzip the whole folder (right-click the zip > Extract All). Do not run it from inside the zip.
2. Open the DrShafiqAqilClinic folder and double-click DrShafiqAqilClinic.exe.
3. The app opens in your web browser at http://127.0.0.1:5000
   A black window also opens. Keep it open while using the app; close it to stop the app.

First start: create the password
   The first time, the app asks you to create a password and choose a security question.
   Remember the answer: "Forgot password?" on the login screen uses it to set a new password.
   The app locks itself after 15 minutes without use; type the password to continue.

If Windows shows "Windows protected your PC"
   Click "More info", then "Run anyway". (The test version is not digitally signed yet.)

Where the data is kept
   In the "data" folder next to DrShafiqAqilClinic.exe (created on first start):
   - lab.db     all patients, cases and results
   - reports\   every printed PDF report
   To start again with no data, close the app and delete the "data" folder.

Installing a new version (keeps all your data)
   1. Close the app.  2. Unzip the new version.
   3. Copy the "data" folder from the old DrShafiqAqilClinic folder into the new one.
   4. Start the new DrShafiqAqilClinic.exe. Keep the old folder for a few days, then delete it.

Requirements
   Windows 10 or 11 with Microsoft Edge (already installed on Windows) - used to create the PDF reports.
   Nothing else needs to be installed.

Barcode scanner
   Click in the search box on the Search page and scan a Patient # or Case # barcode.
