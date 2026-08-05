import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = (ROOT / 'dashboard' / 'index.html').read_text(encoding='utf-8')
README = (ROOT / 'README.md').read_text(encoding='utf-8')
PUBLIC_CONFIG = (ROOT / 'dashboard' / 'phone_share' / 'public_config.py').read_text(encoding='utf-8')

def function_source(name, next_name):
    start = DASHBOARD.index(f'function {name}')
    end = DASHBOARD.index(f'function {next_name}', start)
    return DASHBOARD[start:end]

class PhoneShareDashboardV2ContractTests(unittest.TestCase):
    def test_phone_viewer_uses_canonical_public_repository_name(self):
        canonical_repo = 'https://github.com/abiemann/RobinhoodEquityTradingDashboardViewer'
        canonical_pages = 'https://abiemann.github.io/RobinhoodEquityTradingDashboardViewer/'
        self.assertIn(canonical_repo, README)
        self.assertIn(canonical_pages, README)
        self.assertIn(canonical_pages, PUBLIC_CONFIG)
        self.assertNotIn('github.com/abiemann/RHMRA-Phone', README)
        self.assertNotIn('abiemann.github.io/RHMRA-Phone', README + PUBLIC_CONFIG)

    def test_google_qr_fragment_has_exact_v2_contract(self):
        source = function_source('startGooglePhoneShare', 'startLegacyPhoneShare')
        self.assertIn('''PHONE_SHARE_GOOGLE_FRAGMENT_PROVIDER = 'gdrive';''', DASHBOARD)
        self.assertIn(
            '''viewer.hash = `v=2&provider=${PHONE_SHARE_GOOGLE_FRAGMENT_PROVIDER}&id=${shareId}&key=${base64Url(keyBytes)}`''',
            source,
        )
        self.assertIn('new Uint8Array(32)', source)
        self.assertIn('new Uint8Array(16)', source)

    def test_pairing_persists_only_a_nonextractable_crypto_key(self):
        start = function_source('startGooglePhoneShare', 'startLegacyPhoneShare')
        record = function_source('googlePhonePairingRecord', 'loadGooglePhonePairing')
        validation = function_source('validGooglePhonePairing', 'googlePhonePairingRecord')
        self.assertIn('''{ name: 'AES-GCM' }, false, ['encrypt']''', start)
        self.assertIn('key: pairing.key', record)
        self.assertNotIn('keyBytes', record)
        self.assertNotIn('privateUrl', record)
        self.assertIn('record.key.extractable === false', validation)
        self.assertIn('indexedDB.open(PHONE_SHARE_PAIRING_DB, 1)', DASHBOARD)
        self.assertNotIn('localStorage', DASHBOARD)

    def test_stable_pairing_is_loaded_reused_and_advances_monotonically(self):
        start = function_source('startGooglePhoneShare', 'startLegacyPhoneShare')
        reserve = function_source('reservePhoneShareSequence', 'safeShareText')
        upload = function_source('uploadPhoneShare', 'copyPhoneShareLink')
        self.assertIn('await loadGooglePhonePairing()', start)
        self.assertIn('if (!googlePhonePairing)', start)
        self.assertIn('id: googlePhonePairing.shareId', start)
        self.assertIn('key: googlePhonePairing.key', start)
        self.assertIn('sequence: googlePhonePairing.sequence', start)
        self.assertIn('''db.transaction(PHONE_SHARE_PAIRING_STORE, 'readwrite')''', reserve)
        self.assertIn('const request = store.get(PHONE_SHARE_GOOGLE_PROVIDER)', reserve)
        self.assertIn('Math.max(current.sequence, session.sequence) + 1', reserve)
        self.assertIn('const put = store.put(nextRecord)', reserve)
        self.assertNotIn('await persistGooglePhonePairing', reserve)
        marker = 'const sequence = await reservePhoneShareSequence(session)'
        self.assertIn(marker, upload)
        self.assertLess(upload.index(marker), upload.index('expires_at: session.expiresAt'))

    def test_stop_retains_presented_pairing_but_forget_rotates_it(self):
        stop = function_source('stopPhoneShare', 'forgetGooglePhone')
        forget = function_source('forgetGooglePhone', 'banner')
        self.assertIn('const pairingWasPresented = Boolean(session.lastUploaded)', stop)
        self.assertIn(
            'const incompleteNewPairing = Boolean(session.pairingNeedsPersistence && !pairingWasPresented)',
            stop,
        )
        self.assertIn('your phone remains paired for next time', stop)
        self.assertIn(
            'if (google && incompleteNewPairing) await clearStoredGooglePhonePairing()',
            stop,
        )
        self.assertIn('await clearStoredGooglePhonePairing()', forget)
        self.assertIn('The next share will create a new key and QR code', forget)
        self.assertIn('''method: 'DELETE',''', stop)
        self.assertIn('''method: 'DELETE',''', forget)

    def test_google_auth_failure_retires_only_the_stale_active_session(self):
        recovery = function_source(
            'recoverGooglePhoneShareAuthorization', 'uploadPhoneShare'
        )
        upload = function_source('uploadPhoneShare', 'copyPhoneShareLink')
        stop = function_source('stopPhoneShare', 'forgetGooglePhone')

        self.assertIn('![409, 503].includes(response.status)', recovery)
        self.assertIn('errorDocument = await response.json()', recovery)
        for error in (
            'Google Drive is not connected',
            'Google sign-in service is not configured',
            'Google sign-in service needs attention',
            'Google sign-in service is unavailable',
            'Google Desktop credentials need attention',
            'Google Desktop credentials are not configured',
            'phone sharing is not configured',
        ):
            with self.subTest(error=error):
                self.assertIn(error, recovery)
        self.assertIn(
            'if (!authorizationErrors.has(errorDocument.error)) return false',
            recovery,
        )
        self.assertIn('config = await getPhoneShareConfig()', recovery)
        self.assertIn(
            'Retire the stale session using the safe local error above',
            recovery,
        )
        self.assertIn('config.connected === true', recovery)
        self.assertIn('wipePhoneShareSession(session)', recovery)
        self.assertIn('phoneShareSession = null', recovery)
        self.assertIn('phoneShareConfig = config', recovery)
        self.assertIn('setShareButtonActive(false)', recovery)
        self.assertIn(
            'session.pairingNeedsPersistence && !session.lastUploaded',
            recovery,
        )
        self.assertIn(
            'if (incompleteNewPairing) await clearStoredGooglePhonePairing()',
            recovery,
        )
        self.assertIn('your existing phone pairing was kept', recovery)
        self.assertIn('Any earlier encrypted snapshot will still expire', recovery)
        self.assertIn('Reconnect Google Drive before sharing again', recovery)
        self.assertIn('Google sign-in is temporarily unavailable', recovery)
        self.assertNotIn('Configure the Google Desktop credential', recovery)

        recovery_call = (
            'await recoverGooglePhoneShareAuthorization(response, session)'
        )
        for source in (upload, stop):
            with self.subTest(source=source[:40]):
                self.assertIn(recovery_call, source)
                self.assertLess(
                    source.index(recovery_call),
                    source.index('local relay returned HTTP'),
                )

    def test_share_duration_offers_eight_hours_but_prefers_two(self):
        source = function_source(
            'phoneShareDurationOptions', 'renderShareDialog'
        )
        self.assertIn(
            'durations = [3600, 7200, 14400, 21600, 28800]',
            source,
        )
        self.assertIn('Math.min(PHONE_SHARE_MAX_SECONDS', source)
        self.assertIn('durations.includes(7200) ? 7200', source)

    def test_google_connection_preopens_popup_then_polls_connected_config(self):
        source = function_source(
            'connectGoogleDrivePhoneShare', 'disconnectGoogleDrivePhoneShare'
        )
        popup = source.index('''window.open('about:blank',''')
        request = source.index('''phoneShareLocalFetch('/api/phone-share/connect',''')
        self.assertLess(popup, request)
        self.assertIn('''X-RHMRA-CSRF': phoneShareConfig.csrf_token''', source)
        self.assertIn('authorizationWindow.location.replace', source)
        self.assertIn('config.connected === true', source)
        self.assertIn('config.connection_error', source)
        self.assertLess(
            source.index('config.connection_error'),
            source.index('Google sign-in did not finish within two minutes'),
        )
        self.assertIn('PHONE_SHARE_CONNECT_TIMEOUT_MS', source)

    def test_google_disconnect_is_separated_confirmed_and_preserves_pairing(self):
        render = function_source('renderShareDialog', 'openPhoneShareDialog')
        disconnect = function_source(
            'disconnectGoogleDrivePhoneShare', 'revokePreviousPhoneShare'
        )
        self.assertEqual(
            DASHBOARD.count("id='phone-share-disconnect-google'"), 1
        )
        self.assertIn("class='share-account-controls'", render)
        self.assertIn('Google account connection', render)
        self.assertIn('saved laptop sign-in', render)
        self.assertIn('Your paired phone is kept', render)
        self.assertIn('phone and laptop use the same RHMRA Google permission', render)
        self.assertGreater(
            render.index("id='phone-share-disconnect-google'"),
            render.index("if (phoneShareConfig.connected !== true)"),
        )
        self.assertIn('if (phoneShareSession ||', disconnect)
        self.assertIn('window.confirm(', disconnect)
        self.assertIn(
            "phoneShareLocalFetch('/api/phone-share/disconnect-google'",
            disconnect,
        )
        self.assertIn("method: 'POST'", disconnect)
        self.assertIn(
            "'X-RHMRA-CSRF': phoneShareConfig.csrf_token", disconnect
        )
        self.assertIn("body: '{}'", disconnect)
        self.assertIn('result.warning', disconnect)
        self.assertIn('your paired phone was kept', disconnect)
        self.assertNotIn('clearStoredGooglePhonePairing', disconnect)
        self.assertNotIn('googlePhonePairing = null', disconnect)

    def test_memory_only_google_credentials_have_prominent_warning(self):
        warning = function_source(
            'googleCredentialPersistenceWarning', 'renderShareDialog'
        )
        render = function_source('renderShareDialog', 'openPhoneShareDialog')
        self.assertIn(
            "config?.credential_persistence === 'memory-only'", warning
        )
        self.assertIn("class='share-note share-memory-warning'", warning)
        self.assertIn('Google sign-in is memory-only', warning)
        self.assertIn(
            'Windows protected credential storage is unavailable', warning
        )
        self.assertIn('Reconnect Google Drive after restarting it', warning)
        self.assertGreaterEqual(
            render.count('googleCredentialPersistenceWarning()'), 3
        )

    def test_all_local_phone_share_requests_have_a_bounded_timeout(self):
        helper = function_source('phoneShareLocalFetch', 'getPhoneShareConfig')
        self.assertIn('new AbortController()', helper)
        self.assertIn('PHONE_SHARE_LOCAL_REQUEST_TIMEOUT_MS', helper)
        self.assertIn('controller.abort()', helper)
        self.assertIn('clearTimeout(timeout)', helper)
        self.assertIn("fetch(path, { ...options, signal: controller.signal })", helper)
        self.assertNotIn("fetch('/api/phone-share", DASHBOARD)
        for path in (
            '/api/phone-share/config',
            '/api/phone-share/connect',
            '/api/phone-share/disconnect-google',
        ):
            with self.subTest(path=path):
                self.assertIn("phoneShareLocalFetch('" + path, DASHBOARD)
        self.assertIn("phoneShareLocalFetch('/api/phone-share', {", DASHBOARD)
        self.assertGreaterEqual(
            DASHBOARD.count("phoneShareLocalFetch('/api/phone-share/' +"),
            3,
        )

    def test_legacy_cloudflare_branch_and_session_semantics_remain(self):
        provider = function_source('phoneShareProvider', 'isGoogleDrivePhoneShare')
        legacy = function_source('startLegacyPhoneShare', 'uploadPhoneShare')
        self.assertIn('''? PHONE_SHARE_GOOGLE_PROVIDER : 'cloudflare';''', provider)
        self.assertIn('''provider: 'cloudflare',''', legacy)
        self.assertIn(
            '''new URLSearchParams({ id: shareId, key: base64Url(keyBytes) })''',
            legacy,
        )
        self.assertIn('rememberPhoneShare(shareId, phoneShareSession.expiresAt)', legacy)
        self.assertIn('sessionStorage.setItem(PHONE_SHARE_REVOKE_KEY', DASHBOARD)
        self.assertIn('One-time Cloudflare setup required', DASHBOARD)

    def test_beginner_copy_explains_private_free_account_owned_storage(self):
        render = function_source('renderShareDialog', 'openPhoneShareDialog')
        self.assertIn('your private Google Drive app data', DASHBOARD)
        self.assertNotIn('Google Desktop credential setup required', render)
        self.assertNotIn('RHMRA_PHONE_SHARE_GOOGLE_CREDENTIALS_FILE', render)
        self.assertIn('phoneShareConfig.configuration_error', DASHBOARD)
        self.assertIn('esc(configurationMessage)', DASHBOARD)
        self.assertIn('No setup or paid account', render)
        self.assertIn('you do not need an OAuth file, Cloudflare account', render)
        self.assertIn('stores and logs no tokens', render)
        self.assertIn('never receives dashboard data', render)
        self.assertIn('bucket, API key, paid storage service', DASHBOARD)
        self.assertIn('There is no shared RHMRA dashboard server', DASHBOARD)
        self.assertIn('The OAuth relay never receives dashboard data', DASHBOARD)

    def test_readme_keeps_manual_oauth_file_out_of_end_user_setup(self):
        end_user_setup, developer_override = README.split(
            '#### Developers only: direct OAuth override / relay rollback', 1
        )
        self.assertNotIn('RHMRA_PHONE_SHARE_GOOGLE_CREDENTIALS_FILE', end_user_setup)
        self.assertIn('do **not** download an OAuth JSON file', end_user_setup)
        self.assertIn('same Google account', end_user_setup)
        self.assertIn('stores and logs no authorization codes or tokens', end_user_setup)
        self.assertIn('End users do not need a Cloudflare account', end_user_setup)
        self.assertIn('Google sign-in is memory-only', end_user_setup)
        self.assertIn('Disconnect Google Drive', end_user_setup)
        self.assertIn('always removes the Agent', end_user_setup)
        self.assertIn('does **not** forget the paired phone', end_user_setup)
        self.assertIn('could not confirm remote revocation', end_user_setup)
        self.assertIn('RHMRA_PHONE_SHARE_GOOGLE_CREDENTIALS_FILE', developer_override)
        self.assertIn('developer recovery tool', developer_override)


if __name__ == '__main__':
    unittest.main()
