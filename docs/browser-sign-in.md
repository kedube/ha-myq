# Browser sign-in helper

This is the **Browser sign-in with app verification** method from the
[sign-in guide](sign-in.md). Home Assistant creates the sign-in request and
keeps its secret part. A helper on a computer with Chrome completes MyQ's real
sign-in pages and returns the result to Home Assistant through a private file.
This method has not yet completed a sign-in to MyQ; see
[what is known](sign-in.md#what-is-known).

You need:

- a current Firebase App Check token for the MyQ app, from an authorized native
  source, with more than five minutes of validity left. The helper and the
  integration cannot create one;
- a computer with Google Chrome and Python 3.13, and a checkout of this
  repository.

## Steps

1. **Install the helper** from the repository checkout:

   ```sh
   python3.13 -m venv .venv
   .venv/bin/python -m pip install '.[browser]'
   ```

2. **Save the App Check token** in a private file, readable only by your user.
   Either the raw token or JSON of the form `{"token":"…"}` works. You can skip
   this and type the token at a hidden prompt instead.

3. **Start the sign-in in Home Assistant:** add **MyQ → Browser
   sign-in with app verification**. The form shows a sign-in request. Keep the
   form open; the request expires after ten minutes.

4. **Run the helper**, using your own file paths:

   ```sh
   .venv/bin/python scripts/browser_handoff.py \
     --app-check-file /path/to/app-check.token.json \
     --output /path/to/new-sign-in.token.json
   ```

   Paste the request from Home Assistant when prompted. Chrome opens MyQ's
   sign-in page: enter your password and verification code, and complete any
   browser check. The helper checks the token before starting and again after
   sign-in. It writes the result to a new file readable only by you, and never
   overwrites an existing file.

5. **Finish in Home Assistant:** paste the contents of the result file into
   **Private sign-in result** and submit. Home Assistant checks the result,
   exchanges the sign-in code once, verifies the account and finds your devices.

6. **Delete the result file.** It contains a short-lived sign-in code and the
   App Check token.

## If Chrome's browser check won't complete

Add `--har` to the helper command. It prints the sign-in address to open in
your normal Chrome profile instead:

1. Open Developer Tools → **Network**, enable **Preserve log**, and clear the log.
2. Open the printed address and complete sign-in. The final redirect to
   `com.myqops://android` shows an error because no app handles it; that is
   expected.
3. Export the network log as a HAR file and give its path at the helper's prompt.

The helper reads only the sign-in result that matches Home Assistant's request.
It ignores passwords, cookies and anything else in the file.

## Good to know

- The secret half of the sign-in request (the PKCE verifier) never leaves Home
  Assistant. The result travels by local file and a private form field, not a
  public callback server.
- A Home Assistant restart, a cancelled form or an expired request means
  starting again with a new request.
- If MyQ rejects the app verification (`401.122`), that sign-in request is used
  up; start a new one with a fresh token.
- When reauthenticating, you must sign in to the same MyQ account.
