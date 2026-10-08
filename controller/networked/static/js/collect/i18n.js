// ============================================
// Collect screen - English / Danish strings
// Values sent to the OWLs are slugs (wheat, natural_sun, ...); only the
// labels here are translated. Sentence case, no all-caps.
// ============================================

(function () {
    'use strict';

    const STRINGS = {
        en: {
            tab_record: 'Record', tab_map: 'Map', tab_settings: 'Settings',
            start: 'Start recording', stop: 'Stop recording', ready: 'Ready',
            starting: 'Starting', no_owls: 'No OWLs connected',
            images: 'Images', speed: 'Speed', shutter: 'Shutter', gain: 'Gain', free: 'Free',
            offline: 'Offline', not_recording: 'Not recording',
            meta_hint: 'Tap Start recording to choose crop and conditions',
            all_ok: 'All checks passing', more_warnings: '{n} more',
            latest: 'latest', previous: 'previous', waiting_image: 'Waiting for the first image',
            video_clip: 'Video {s} s',
            // Modal
            m_title: 'What are you recording?', crop: 'Crop', season: 'Season', weather: 'Weather',
            lighting: 'Light', purpose: 'Purpose', notes: 'Notes', notes_ph: 'Optional. Tap to type',
            other: 'Other', other_ph: 'Type the crop', cancel: 'Cancel', ok: 'OK',
            confirm_stop: 'Stop recording on all OWLs?', confirm_yes: 'Stop', confirm_no: 'Keep recording',
            // Map
            map_images: 'Images this run', map_spacing: 'Spacing', map_centre: 'Centre on images',
            map_none: 'Image locations appear here once recording starts with GPS.',
            track: 'Image locations',
            // Settings
            s_sessions: 'Sessions and upload', s_capture: 'Capture', s_camera: 'Camera',
            s_cloud: 'Noktura', s_full: 'Full dashboard', s_language: 'Language',
            session: 'Session', size: 'Size', status: 'Status', refresh: 'Refresh list',
            upload: 'Upload selected', nothing_selected: 'Nothing selected', selected: '{n} selected, {mb} MB',
            upload_hint: "Uploads use the router's mobile data. Recording can continue while they run.",
            no_sessions: 'No sessions yet. Tap Refresh list after recording.',
            st_on_owl: 'On OWL', st_requested: 'Requested', st_accepted: 'Queued in Noktura',
            st_rejected: 'Not accepted', st_uploading: 'Uploading {p}%', st_uploaded: 'Uploaded',
            st_failed: 'Upload failed',
            upload_already: 'Already requested. Wait for Noktura to answer before trying again.',
            cloud_needed: 'Connect to Noktura first (Settings, Noktura).',
            record_as: 'Record as', stills: 'Still images', video: 'Video clip',
            spacing_mode: 'Take images', by_distance: 'By distance', by_time: 'By time',
            every_m: 'Every (metres)', every_s: 'Without GPS, every (seconds)',
            quality: 'Image quality (JPEG)', video_len: 'Video clip length (5 to 60 s)',
            smear: 'Blur limit (mm of movement per image)', shutter_cap: 'Limit shutter time for speed',
            on: 'On', off: 'Off', save: 'Save', saved: 'Saved',
            saved_restart: 'Saved. Restart the OWLs to apply it.',
            capture_hint: 'Distance spacing uses GPS speed. The shutter limit raises gain instead of slowing the shutter and switches off if GPS is lost.',
            cam_all: 'Applies to all OWLs', resolution: 'Resolution', brightness: 'Brightness',
            darker: 'Darker', brighter: 'Brighter', wb: 'White balance', wb_auto: 'Auto',
            wb_daylight: 'Daylight', wb_cloudy: 'Cloudy', wb_manual: 'Locked',
            calibrate: 'Calibrate white balance', calibrate_hint: 'Point the cameras at the crop in the current light, then tap.',
            gain_limit: 'Gain limit', auto: 'Auto', rotation: 'Rotation', new_tag: 'New',
            cam_hint: 'Resolution and rotation restart the OWLs (about 20 s). Brightness, white balance and gain limit apply straight away. A lower gain limit means less noise; the screen warns if images get too dark.',
            restart_needed: 'Restart the OWLs to apply the new camera settings.', restart_owls: 'Restart OWLs',
            cam_waiting: 'Reading camera settings from the OWLs',
            cloud_status: 'Connection', cloud_site: 'Site', cloud_owls: 'Internet from each OWL',
            cloud_not_set: 'Not set up', cloud_connected: 'Connected', cloud_offline: 'Offline',
            cloud_checking: 'Checking', cloud_portal: 'Noktura on the web: {url}',
            paste_label: "Connect or replace: paste the command from Noktura's one-time link",
            connect: 'Connect', replace: 'Replace the current connection',
            paste_hint: 'Easiest from a laptop on the same network: open https://<controller address>/collect',
            connected_ok: 'Connected to Noktura as {site}',
            full_hint: 'Opens the full OWL Controller dashboard (detection, config, AI). Detection stays off while this screen is the default.',
            open_full: 'Open full dashboard',
            pill_not_set: 'Noktura not set up', pill_offline: 'Noktura offline',
            pill_connected: 'Noktura connected', pill_uploading: 'Uploading {p}%',
            unknown: 'Unknown', ok_short: 'ok', no_route: 'no internet',
            // Warnings: {owl} is replaced with the OWL name
            w_offline: '{owl} is offline. Check its power and network cable.',
            w_storage_low: '{owl} is low on storage ({free}). Upload or delete old sessions.',
            w_recording_blocked: '{owl} cannot record: {reason}.',
            w_video_limit: '{owl} stopped: the video clip reached {s} s.',
            w_clock: "{owl}'s clock is not set. Image times may be wrong until it reaches the internet.",
            w_no_internet: '{owl} cannot reach the internet, so it cannot upload.',
            w_no_gps: 'No GPS fix. Images are spaced by time and have no location.',
            w_blur_risk: '{owl}: blur risk, {mm} mm of movement per image. Slow to about {kmh} km/h.',
            w_blur_risk_capped: '{owl}: blur risk even at the shortest shutter. Slow to about {kmh} km/h.',
            w_soft: '{owl}: images are softer than at the start. Check the lens is clean.',
            w_too_dark: '{owl}: images are too dark. Raise brightness or the gain limit.',
            w_overexposed: '{owl}: images are overexposed. Lower brightness.',
            w_not_saving: '{owl} has not saved an image for {s} s while moving.',
            w_not_recording: '{owl} is not recording. Stop and start recording again.',
            // Presets
            crop_wheat: 'Wheat', crop_barley: 'Barley', crop_canola: 'Oilseed rape', crop_rye: 'Rye',
            crop_oats: 'Oats', crop_triticale: 'Triticale', crop_sugar_beet: 'Sugar beet', crop_maize: 'Maize',
            crop_grass_clover: 'Grass/clover', crop_peas: 'Peas', crop_faba_beans: 'Faba beans',
            crop_potatoes: 'Potatoes', crop_fallow: 'Fallow',
            season_winter: 'Winter', season_spring: 'Spring',
            weather_dry: 'Dry', weather_dew: 'Damp or dew', weather_wet: 'Wet', weather_windy: 'Windy',
            lighting_natural_sun: 'Sunny', lighting_natural_partial: 'Partly cloudy',
            lighting_natural_overcast: 'Overcast',
            purpose_herbicide: 'Herbicide', purpose_fungicide: 'Fungicide',
            purpose_insecticide: 'Insecticide', purpose_survey: 'Survey only',
        },
        da: {
            tab_record: 'Optag', tab_map: 'Kort', tab_settings: 'Indstillinger',
            start: 'Start optagelse', stop: 'Stop optagelse', ready: 'Klar',
            starting: 'Starter', no_owls: 'Ingen OWLs forbundet',
            images: 'Billeder', speed: 'Hastighed', shutter: 'Lukker', gain: 'Forstærkning', free: 'Ledig',
            offline: 'Offline', not_recording: 'Optager ikke',
            meta_hint: 'Tryk Start optagelse for at vælge afgrøde og forhold',
            all_ok: 'Alle kontroller er i orden', more_warnings: '{n} mere',
            latest: 'seneste', previous: 'forrige', waiting_image: 'Venter på det første billede',
            video_clip: 'Video {s} s',
            m_title: 'Hvad optager du?', crop: 'Afgrøde', season: 'Sæson', weather: 'Vejr',
            lighting: 'Lys', purpose: 'Formål', notes: 'Noter', notes_ph: 'Valgfrit. Tryk for at skrive',
            other: 'Andet', other_ph: 'Skriv afgrøden', cancel: 'Annuller', ok: 'OK',
            confirm_stop: 'Stop optagelsen på alle OWLs?', confirm_yes: 'Stop', confirm_no: 'Fortsæt optagelsen',
            map_images: 'Billeder i denne kørsel', map_spacing: 'Billedafstand', map_centre: 'Centrér på billeder',
            map_none: 'Billedernes positioner vises her, når optagelsen starter med GPS.',
            track: 'Billedpositioner',
            s_sessions: 'Sessioner og upload', s_capture: 'Optagelse', s_camera: 'Kamera',
            s_cloud: 'Noktura', s_full: 'Fuldt dashboard', s_language: 'Sprog',
            session: 'Session', size: 'Størrelse', status: 'Status', refresh: 'Opdatér listen',
            upload: 'Upload valgte', nothing_selected: 'Intet valgt', selected: '{n} valgt, {mb} MB',
            upload_hint: 'Upload bruger routerens mobildata. Optagelsen kan fortsætte imens.',
            no_sessions: 'Ingen sessioner endnu. Tryk Opdatér listen efter optagelse.',
            st_on_owl: 'På OWL', st_requested: 'Anmodet', st_accepted: 'I kø hos Noktura',
            st_rejected: 'Ikke accepteret', st_uploading: 'Uploader {p}%', st_uploaded: 'Uploadet',
            st_failed: 'Upload mislykkedes',
            upload_already: 'Allerede anmodet. Vent på svar fra Noktura, før du prøver igen.',
            cloud_needed: 'Forbind til Noktura først (Indstillinger, Noktura).',
            record_as: 'Optag som', stills: 'Stillbilleder', video: 'Videoklip',
            spacing_mode: 'Tag billeder', by_distance: 'Efter afstand', by_time: 'Efter tid',
            every_m: 'Hver (meter)', every_s: 'Uden GPS, hver (sekunder)',
            quality: 'Billedkvalitet (JPEG)', video_len: 'Videoklip længde (5 til 60 s)',
            smear: 'Slørgrænse (mm bevægelse pr. billede)', shutter_cap: 'Begræns lukkertid efter hastighed',
            on: 'Til', off: 'Fra', save: 'Gem', saved: 'Gemt',
            saved_restart: 'Gemt. Genstart OWLs for at bruge det.',
            capture_hint: 'Afstand bruger GPS-hastighed. Lukkergrænsen hæver forstærkningen i stedet for en langsommere lukker og slås fra, hvis GPS mistes.',
            cam_all: 'Gælder alle OWLs', resolution: 'Opløsning', brightness: 'Lysstyrke',
            darker: 'Mørkere', brighter: 'Lysere', wb: 'Hvidbalance', wb_auto: 'Auto',
            wb_daylight: 'Dagslys', wb_cloudy: 'Overskyet', wb_manual: 'Låst',
            calibrate: 'Kalibrér hvidbalance', calibrate_hint: 'Ret kameraerne mod afgrøden i det aktuelle lys, og tryk så.',
            gain_limit: 'Grænse for forstærkning', auto: 'Auto', rotation: 'Rotation', new_tag: 'Ny',
            cam_hint: 'Opløsning og rotation genstarter OWLs (ca. 20 s). Lysstyrke, hvidbalance og forstærkningsgrænse gælder med det samme. En lavere grænse giver mindre støj; skærmen advarer, hvis billederne bliver for mørke.',
            restart_needed: 'Genstart OWLs for at bruge de nye kameraindstillinger.', restart_owls: 'Genstart OWLs',
            cam_waiting: 'Læser kameraindstillinger fra OWLs',
            cloud_status: 'Forbindelse', cloud_site: 'Site', cloud_owls: 'Internet fra hver OWL',
            cloud_not_set: 'Ikke sat op', cloud_connected: 'Forbundet', cloud_offline: 'Offline',
            cloud_checking: 'Kontrollerer', cloud_portal: 'Noktura på nettet: {url}',
            paste_label: 'Forbind eller erstat: indsæt kommandoen fra Nokturas engangslink',
            connect: 'Forbind', replace: 'Erstat den nuværende forbindelse',
            paste_hint: 'Nemmest fra en bærbar på samme netværk: åbn https://<controllerens adresse>/collect',
            connected_ok: 'Forbundet til Noktura som {site}',
            full_hint: 'Åbner det fulde OWL Controller-dashboard (detektion, konfiguration, AI). Detektion forbliver slået fra, mens denne skærm er standard.',
            open_full: 'Åbn fuldt dashboard',
            pill_not_set: 'Noktura ikke sat op', pill_offline: 'Noktura offline',
            pill_connected: 'Noktura forbundet', pill_uploading: 'Uploader {p}%',
            unknown: 'Ukendt', ok_short: 'ok', no_route: 'intet internet',
            w_offline: '{owl} er offline. Tjek strøm og netværkskabel.',
            w_storage_low: '{owl} har lidt plads tilbage ({free}). Upload eller slet gamle sessioner.',
            w_recording_blocked: '{owl} kan ikke optage: {reason}.',
            w_video_limit: '{owl} stoppede: videoklippet nåede {s} s.',
            w_clock: '{owl}s ur er ikke indstillet. Billedtider kan være forkerte, indtil den når internettet.',
            w_no_internet: '{owl} kan ikke nå internettet og kan derfor ikke uploade.',
            w_no_gps: 'Intet GPS-fix. Billeder tages efter tid og har ingen position.',
            w_blur_risk: '{owl}: risiko for sløring, {mm} mm bevægelse pr. billede. Sænk til ca. {kmh} km/t.',
            w_blur_risk_capped: '{owl}: risiko for sløring selv med korteste lukker. Sænk til ca. {kmh} km/t.',
            w_soft: '{owl}: billederne er mindre skarpe end ved start. Tjek at linsen er ren.',
            w_too_dark: '{owl}: billederne er for mørke. Hæv lysstyrken eller forstærkningsgrænsen.',
            w_overexposed: '{owl}: billederne er overeksponerede. Sænk lysstyrken.',
            w_not_saving: '{owl} har ikke gemt et billede i {s} s under kørsel.',
            w_not_recording: '{owl} optager ikke. Stop og start optagelsen igen.',
            crop_wheat: 'Hvede', crop_barley: 'Byg', crop_canola: 'Raps', crop_rye: 'Rug',
            crop_oats: 'Havre', crop_triticale: 'Triticale', crop_sugar_beet: 'Sukkerroer', crop_maize: 'Majs',
            crop_grass_clover: 'Græs/kløver', crop_peas: 'Ærter', crop_faba_beans: 'Hestebønner',
            crop_potatoes: 'Kartofler', crop_fallow: 'Brak',
            season_winter: 'Vinter', season_spring: 'Vår',
            weather_dry: 'Tørt', weather_dew: 'Fugtigt eller dug', weather_wet: 'Vådt', weather_windy: 'Blæsende',
            lighting_natural_sun: 'Sol', lighting_natural_partial: 'Delvist skyet',
            lighting_natural_overcast: 'Overskyet',
            purpose_herbicide: 'Herbicid', purpose_fungicide: 'Fungicid',
            purpose_insecticide: 'Insekticid', purpose_survey: 'Kun registrering',
        },
    };

    let lang = 'en';

    function t(key, values) {
        let text = (STRINGS[lang] && STRINGS[lang][key]) || STRINGS.en[key];
        if (text === undefined) {
            // A preset slug from [Collect] with no translation: show it readable
            const slug = key.replace(/^(crop|season|weather|lighting|purpose)_/, '');
            text = slug.replace(/_/g, ' ');
            text = text.charAt(0).toUpperCase() + text.slice(1);
        }
        if (values) {
            Object.keys(values).forEach(function (k) {
                text = text.split('{' + k + '}').join(values[k] === null || values[k] === undefined ? '' : values[k]);
            });
        }
        return text;
    }

    function setLang(next) { lang = STRINGS[next] ? next : 'en'; }
    function getLang() { return lang; }

    function apply(root) {
        (root || document).querySelectorAll('[data-i18n]').forEach(function (el) {
            el.textContent = t(el.getAttribute('data-i18n'));
        });
        (root || document).querySelectorAll('[data-i18n-ph]').forEach(function (el) {
            el.placeholder = t(el.getAttribute('data-i18n-ph'));
        });
        document.documentElement.lang = lang;
    }

    window.CollectI18n = { t: t, setLang: setLang, getLang: getLang, apply: apply };
})();
