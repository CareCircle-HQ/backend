// Copy this file to `config.js` and fill in your service token.
// `config.js` is gitignored so the secret is not committed.
//
// Generate the token on the backend with:
//   python manage.py create_service_token --staff
window.EXT_CONFIG = {
  backendUrl: "http://127.0.0.1:8000",
  // Static service-account token. Sent as: Authorization: Token <apiToken>
  apiToken: "<paste service token here>",
  authScheme: "Token",
};
