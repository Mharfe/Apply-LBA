"""
Automatisation des candidatures spontanées sur La Bonne Alternance (LBA).
Utilise Playwright pour piloter un navigateur réel.
"""

import asyncio
import json
import re
import threading
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

BASE_URL = "https://labonnealternance.apprentissage.beta.gouv.fr"

CITIES_DEFAULT = [
    # {"name": "Clermont-Ferrand", "address": "Clermont-Ferrand 63000", "lat": 45.7772, "lon": 3.0870},

    # Priority 1 - Best opportunity + acceptable travel time
    {"name": "Paris",            "address": "Paris 75000",            "lat": 48.8566, "lon": 2.3522},
    {"name": "Lyon",             "address": "Lyon 69001",             "lat": 45.7640, "lon": 4.8357},

    # Priority 2 - Secondary nearby markets
    {"name": "Saint-Étienne",    "address": "Saint-Étienne 42000",    "lat": 45.4397, "lon": 4.3872},
    {"name": "Valence",          "address": "Valence 26000",          "lat": 44.9334, "lon": 4.8924},

    # Optional - low priority / fallback
    {"name": "Vichy",            "address": "Vichy 03200",            "lat": 46.1286, "lon": 3.4264},
    {"name": "Montluçon",        "address": "Montluçon 03100",        "lat": 46.3400, "lon": 2.6020},
]

JOB_SEARCHES_DEFAULT = [
    {
        "name": "Développement web, intégration",
        "romes": "M1805,M1855,M1825,M1834,M1861,E1210,E1405,M1865,M1877,M1886,M1887",
    },
    {
        "name": "Informatique et systèmes d'information",
        "romes": "M1801,M1802,M1803,M1810,M1811,M1807",
    },
    {
        "name": "Programmation, développement logiciel",
        "romes": "M1805,M1855",
    },
    {
        "name": "Cybersécurité, réseau",
        "romes": "M1801,M1812",
    },
]


class LBAAutomation:
    """Automatise l'envoi de candidatures spontanées sur LBA."""

    def __init__(self, config: dict, stop_event: threading.Event, callbacks: dict = None):
        self.config = config
        self.stop_event = stop_event
        self.callbacks = callbacks or {}
        self.sent_file = Path("sent_applications.json")
        self.sent: list = self._load_sent()
        self.stats: dict = {
            "status": "running",
            "sent_today": 0,
            "skipped": 0,
            "errors": 0,
            "current_city": "",
            "current_job": "",
            "current_company": "",
        }

    # ------------------------------------------------------------------ helpers

    def _load_sent(self) -> list:
        if self.sent_file.exists():
            try:
                return json.loads(self.sent_file.read_text(encoding="utf-8"))
            except Exception:
                return []
        return []

    def _save_sent(self) -> None:
        self.sent_file.write_text(
            json.dumps(self.sent, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def _already_sent(self, siret: str) -> bool:
        today = datetime.now().strftime("%Y-%m-%d")
        return any(
            s.get("siret") == siret and s.get("date") == today
            for s in self.sent
        )

    def _record_sent(self, siret: str, company: str, city: str, job: str) -> None:
        self.sent.append({
            "siret": siret,
            "company": company,
            "city": city,
            "job": job,
            "date": datetime.now().strftime("%Y-%m-%d"),
            "time": datetime.now().strftime("%H:%M:%S"),
        })
        self._save_sent()

    def _log(self, message: str, level: str = "info") -> None:
        now = datetime.now().strftime("%H:%M:%S")
        print(f"[{now}] [{level.upper()}] {message}")
        if cb := self.callbacks.get("log"):
            cb({"time": now, "message": message, "level": level})

    def _update_stats(self, **kwargs) -> None:
        self.stats.update(kwargs)
        if cb := self.callbacks.get("status"):
            cb(self.stats.copy())

    def _stopped(self) -> bool:
        return self.stop_event.is_set()

    def _qs(self, params: dict) -> str:
        return "&".join(f"{k}={quote(str(v))}" for k, v in params.items())

    # ------------------------------------------------------------------ main

    async def run(self) -> None:
        from playwright.async_api import async_playwright

        self._log("🚀 Démarrage de l'automatisation LBA")
        headless = self.config.get("headless", False)
        delay = int(self.config.get("delay_between_applications", 3))

        city_by_name = {c["name"]: c for c in CITIES_DEFAULT}
        selected_city_names = self.config.get("selected_cities") or []
        cities = [city_by_name[name] for name in selected_city_names if name in city_by_name]
        if not cities:
            cities = CITIES_DEFAULT
        jobs = self.config.get("job_searches", JOB_SEARCHES_DEFAULT)

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=headless)
            ctx = await browser.new_context(
                viewport={"width": 1280, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
            )

            for job in jobs:
                if self._stopped():
                    break
                for city in cities:
                    if self._stopped():
                        break
                    self._update_stats(
                        current_city=city["name"], current_job=job["name"]
                    )
                    # _search_companies garde la page résultats ouverte et la renvoie
                    search_page, entries = await self._search_companies(ctx, job, city)
                    try:
                        for siret, href in entries:
                            if self._stopped():
                                break
                            if self._already_sent(siret):
                                self.stats["skipped"] += 1
                                self._log(f"⏭️  Déjà candidaté: {siret}")
                                continue
                            company_url = href if href.startswith("http") else f"{BASE_URL}{href}"
                            # Ouvrir un nouvel onglet pour la candidature
                            apply_page = await ctx.new_page()
                            try:
                                result = await self._apply_on_page(apply_page, siret, company_url, city["name"], job["name"])
                            finally:
                                await apply_page.close()
                            # Si limite atteinte → on stoppe tout
                            if result == "limit":
                                self._log("🛑 Limite de candidatures atteinte — arrêt total")
                                self.stop_event.set()
                                break
                            await asyncio.sleep(delay)
                    finally:
                        # Fermer la page résultats après avoir tout traité
                        try:
                            await search_page.close()
                        except Exception:
                            pass

            await browser.close()

        self._update_stats(status="stopped")
        self._log("✅ Automatisation terminée")

    # ------------------------------------------------------------------ search

    async def _search_companies(self, ctx, job: dict, city: dict) -> list:
        """
        Reproduit exactement le geste humain sur LBA :
          1. Page d'accueil
          2. Cocher UNIQUEMENT le filtre "Emploi"
          3. Métier → dropdown → sélection
          4. Ville  → dropdown → sélection
          5. Cliquer Rechercher → attendre navigation
          6. Sur la page résultats : décocher Formation/Alternance si présents
          7. Scroller jusqu'à ce que plus rien de nouveau n'apparaît
          8. Collecter les cartes avec badge CANDIDATURE SPONTANÉE
             → récupérer le href via card.querySelector('.fr-card__title a')
        """
        from playwright.async_api import TimeoutError as PwTimeout

        self._log(f"🔍 {job['name']} → {city['name']}")
        page = await ctx.new_page()
        entries: list[tuple[str, str]] = []

        async def _check_only_emploi(pg):
            """
            Sur LBA les IDs sont précis :
              #displayedItemTypes-Emplois   → doit être COCHÉ
              #displayedItemTypes-Formations → doit être DÉCOCHÉ
            On utilise les IDs et name directement, pas le texte du label.
            """
            await asyncio.sleep(1)

            # 1. Décocher Formations via son ID exact
            for sel in [
                "#displayedItemTypes-Formations",
                'input[name="Formations"]',
            ]:
                try:
                    el = await pg.query_selector(sel)
                    if el and await el.is_checked():
                        # Le clic direct est bloqué par "readonly" → JS click
                        await pg.evaluate("(el) => el.click()", el)
                        self._log("  ☐  Formations décoché")
                        await asyncio.sleep(0.5)
                        break
                except Exception:
                    pass

            # 2. Cocher Emplois via son ID exact
            for sel in [
                "#displayedItemTypes-Emplois",
                'input[name="Emplois"]',
            ]:
                try:
                    el = await pg.query_selector(sel)
                    if el and not await el.is_checked():
                        await pg.evaluate("(el) => el.click()", el)
                        self._log("  ☑️  Emplois coché")
                        await asyncio.sleep(0.5)
                    break
                except Exception:
                    pass

        try:
            # ── 1. Page d'accueil ────────────────────────────────────────
            await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=30_000)
            await asyncio.sleep(2)

            # ── 2. Filtre Emploi sur la page d'accueil ───────────────────
            await _check_only_emploi(page)

            # ── 3. Remplir le champ métier ───────────────────────────────
            job_input = page.locator(
                "input#metier, "
                'input[placeholder*="métier"], '
                'input[placeholder*="formation"]'
            ).first

            if await job_input.count() > 0:
                await job_input.click()
                await job_input.fill("")
                search_term = " ".join(job["name"].split(",")[0].split()[:3])[:20]
                self._log(f"  ✏️  Métier : {search_term!r}")
                await job_input.type(search_term, delay=90)
                await asyncio.sleep(2.5)

                keywords = [w.lower() for w in job["name"].split() if len(w) > 3]
                matched = False
                for opt_sel in ['[role="option"]', '[role="listbox"] li', 'ul[role="listbox"] li']:
                    opts = page.locator(opt_sel)
                    count = await opts.count()
                    for i in range(count):
                        opt = opts.nth(i)
                        if not await opt.is_visible():
                            continue
                        txt = (await opt.inner_text()).strip().lower()
                        if any(kw in txt for kw in keywords):
                            await opt.click()
                            matched = True
                            self._log(f"  ✅  Option métier sélectionnée : {txt[:50]}")
                            await asyncio.sleep(0.6)
                            break
                    if matched:
                        break

                if not matched:
                    first_opt = page.locator('[role="option"]').first
                    if await first_opt.count() > 0 and await first_opt.is_visible():
                        txt = await first_opt.inner_text()
                        await first_opt.click()
                        self._log(f"  ☑️  Première option : {txt.strip()[:50]}")
                    else:
                        await job_input.press("Enter")
                    await asyncio.sleep(0.6)
            else:
                self._log("  ⚠️  Champ métier introuvable", "warning")

            # ── 4. Remplir le champ ville ────────────────────────────────
            city_input = None
            for sel in [
                "input#lieu",
                'input[placeholder*="commune"]',
                'input[placeholder*="département"]',
                'input[placeholder*="localisation"]',
                'input[aria-label*="lieu"]',
            ]:
                loc = page.locator(sel).first
                if await loc.count() > 0:
                    city_input = loc
                    break

            if city_input:
                await city_input.click()
                await city_input.fill("")
                self._log(f"  🏙️  Ville : {city['name']}")
                await city_input.type(city["name"][:8], delay=90)
                await asyncio.sleep(2)
                first_city = page.locator('[role="option"]').first
                if await first_city.count() > 0 and await first_city.is_visible():
                    txt = await first_city.inner_text()
                    await first_city.click()
                    self._log(f"  ✅  Ville sélectionnée : {txt.strip()[:40]}")
                else:
                    await city_input.press("Enter")
                await asyncio.sleep(0.6)
            else:
                self._log("  ⚠️  Champ ville introuvable", "warning")

            # ── 5. Cliquer Rechercher + attendre la navigation ───────────
            search_btn = page.locator('button:has-text("Rechercher"), button[type="submit"]').first
            async with page.expect_navigation(wait_until="domcontentloaded", timeout=30_000):
                if await search_btn.count() > 0:
                    await search_btn.click()
                else:
                    await page.keyboard.press("Enter")
            self._log(f"  → Page résultats : {page.url[:80]}")
            # Attendre que React finisse de rendre les résultats
            try:
                await page.wait_for_load_state("networkidle", timeout=20_000)
            except PwTimeout:
                pass
            await asyncio.sleep(3)

            # ── 6. Décocher Formations sur la page résultats ─────────────
            await _check_only_emploi(page)
            # Attendre que le filtre soit pris en compte et la liste re-rendue
            try:
                await page.wait_for_load_state("networkidle", timeout=10_000)
            except PwTimeout:
                pass
            await asyncio.sleep(2)

            # ── 7. Attendre qu'au moins une carte apparaisse ─────────────
            # Attente potentiellement illimitée: on ne quitte pas tant qu'aucune
            # offre n'est visible, sauf si l'utilisateur demande l'arrêt.
            self._log("  ⏳ Attente des offres (sans limite de temps)…")
            while True:
                if self._stopped():
                    self._log("  🛑 Arrêt demandé pendant l'attente des offres", "warning")
                    return page, []
                try:
                    await page.wait_for_selector(".fr-card", timeout=15_000)
                    break
                except PwTimeout:
                    self._log(
                        f"  ⏳ Toujours en attente des offres pour {city['name']}…",
                        "info",
                    )

            # ── 8–9. Scroll + collecte incrémentale ─────────────────────────
            # LBA utilise une liste virtualisée : le DOM ne contient qu’environ
            # 20–30 `.fr-card` à la fois. Compter les cartes ou « tout charger »
            # une seule fois ne suffit pas — il faut fusionner les SIRET à chaque
            # défilement jusqu’à ce qu’aucune nouvelle carte éligible n’apparaisse.

            JS_COLLECT = r"""
() => {
    const results = [];
    const seen = new Set();
    const debug = [];

    const cards = Array.from(document.querySelectorAll('.fr-card'));
    const debugTotal = cards.length;

    cards.forEach((card, idx) => {
        const text = (card.textContent || '').toLowerCase();

        // Debug : premières 3 cartes
        if (idx < 3) {
            debug.push(text.substring(0, 200));
        }

        // Ignorer les cartes "déjà postulé"
        if (text.includes('vous avez d') && text.includes('postul')) return;

        // CONDITION IMPÉRATIVE : "candidature simplifiée" dans le texte
        const hasSimplifiee = text.includes('candidature simplifi');
        // BONUS : badge spontanée (aria-describedby ou texte)
        const hasSpontanee = !!card.querySelector('[aria-describedby*="candidature-spontanee-tag-"]')
                          || text.includes('candidature spontan');

        // On prend la carte si elle a AU MOINS simplifiée
        if (!hasSimplifiee) return;

        // Récupérer le lien recruteurs_lba
        let a = card.querySelector('a[href*="recruteurs_lba"]');
        if (!a) a = card.querySelector('.fr-card__title a');
        if (!a) a = card.querySelector('h3 a');
        if (!a || !a.href) return;

        const href = a.href;
        const m = href.match(/recruteurs_lba\/(\d+)\//);
        if (!m) return;
        const siret = m[1];
        if (seen.has(siret)) return;
        seen.add(siret);

        const type = hasSpontanee ? 'spontanee' : 'simplifiee';
        results.push({ siret, href, type });
    });

    return { total: debugTotal, entries: results, debug: debug };
}
"""
            JS_SCROLL_STEP = r"""
() => {
    const step = Math.max(300, Math.floor(window.innerHeight * 0.88));
    for (const el of document.querySelectorAll('main, [role="main"], article, div')) {
        try {
            const st = getComputedStyle(el);
            if (!/auto|scroll|overlay/.test(st.overflowY || '')) continue;
            if (el.scrollHeight <= el.clientHeight + 80) continue;
            const t = el.scrollTop;
            el.scrollTop = Math.min(t + step, el.scrollHeight - el.clientHeight);
            if (el.scrollTop !== t) return;
        } catch (e) {}
    }
    const maxY = Math.max(
        document.documentElement.scrollHeight,
        document.body.scrollHeight
    ) - window.innerHeight;
    window.scrollBy(0, step);
    if (window.scrollY + window.innerHeight < maxY - 8) return;
    window.scrollTo(0, Math.max(0, maxY));
}
"""

            accumulated: dict[str, dict] = {}
            stagnation = 0
            max_stagnation = 28
            prev_unique = 0
            first_debug: list | None = None

            self._log("  📜 Défilement + collecte incrémentale (liste virtualisée)…")
            while stagnation < max_stagnation:
                js_result = await page.evaluate(JS_COLLECT)
                if first_debug is None:
                    first_debug = js_result.get("debug", [])

                for item in js_result["entries"]:
                    s = item["siret"]
                    if s not in accumulated:
                        accumulated[s] = {"href": item["href"], "type": item["type"]}

                n = len(accumulated)
                if n > prev_unique:
                    self._log(
                        f"  📜 {n} entreprise(s) « candidature simplifiée » "
                        f"({js_result['total']} carte(s) dans le DOM)"
                    )
                    prev_unique = n
                    stagnation = 0
                else:
                    stagnation += 1

                for btn_text in ["Voir plus", "Charger plus", "Afficher plus"]:
                    try:
                        btn = page.locator(f'button:has-text("{btn_text}")').first
                        if await btn.count() > 0 and await btn.is_visible():
                            await btn.click()
                            self._log(f"  📜 Clic sur « {btn_text} »")
                            await asyncio.sleep(2)
                            stagnation = 0
                    except Exception:
                        pass

                await page.evaluate(JS_SCROLL_STEP)
                try:
                    last_card = page.locator(".fr-card").last
                    await last_card.scroll_into_view_if_needed(timeout=2500)
                except Exception:
                    pass
                await asyncio.sleep(0.75)

            raw_entries = [
                {"siret": s, "href": v["href"], "type": v["type"]}
                for s, v in accumulated.items()
            ]
            total_cards = await page.locator(".fr-card").count()

            self._log(
                f"  🔎 Fin collecte — {len(raw_entries)} SIRET unique(s), "
                f"{total_cards} carte(s) .fr-card dans le DOM à l’arrêt"
            )
            for i, dtxt in enumerate(first_debug or []):
                self._log(f"  🐛 Carte {i}: {dtxt[:150]}…")

            max_detail_logs = 40
            for idx, item in enumerate(raw_entries):
                siret = item["siret"]
                href = item["href"]
                kind = item["type"]
                entries.append((siret, href))
                if idx < max_detail_logs:
                    label = "SPONTANÉE" if kind == "spontanee" else "SIMPLIFIÉE"
                    short = href[:90] + "…" if len(href) > 90 else href
                    self._log(f"  🏢 [{label}] SIRET {siret} → {short}")
            if len(raw_entries) > max_detail_logs:
                self._log(f"  … (+{len(raw_entries) - max_detail_logs} autres, détail tronqué dans les logs)")

            self._log(
                f"  → {len(entries)} entreprise(s) : "
                f"{sum(1 for i in raw_entries if i['type']=='spontanee')} spontanée(s), "
                f"{sum(1 for i in raw_entries if i['type']=='simplifiee')} simplifiée(s)"
            )

            # ── 10. Remonter en haut de page avant de traiter ────────────
            await page.evaluate("window.scrollTo(0, 0)")
            self._log("  ⬆️  Retour en haut de page")

        except Exception as exc:
            self._log(f"  ❌ Recherche: {exc}", "error")
            self.stats["errors"] += 1

        # NE PAS fermer la page ici — l'appelant la gère
        return page, entries

    # ------------------------------------------------------------------ apply

    async def _apply_on_page(
        self, page, siret: str, url: str, city_name: str, job_name: str
    ) -> None:
        """Applique sur la page déjà ouverte (un onglet dédié)."""
        from playwright.async_api import TimeoutError as PwTimeout

        self._update_stats(current_company=siret)
        self._log(f"📝 SIRET: {siret}")
        company_name = siret
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            await asyncio.sleep(4)

            # ── Détecter la limite de candidatures atteinte ───────────
            body_text = await page.inner_text("body")
            if "vous avez atteint" in body_text.lower():
                self._log("  🛑 LIMITE ATTEINTE — arrêt immédiat", "warning")
                self._log(f"     {body_text.strip()[:150]}")
                return "limit"

            # Try to get company name from the page
            for sel in [
                ".mui-gbrs06 > span:first-child",
                "[class*='gbrs06'] span",
                "h1",
            ]:
                try:
                    el = await page.query_selector(sel)
                    if el:
                        txt = (await el.inner_text()).strip()
                        if txt:
                            company_name = txt
                            break
                except Exception:
                    pass

            self._log(f"  🏢 {company_name}")
            self._update_stats(current_company=company_name)

            # Click the application button
            btn = await page.query_selector('[data-testid="postuler-button"]')
            if not btn:
                # Try fallback text selector
                btn = await page.query_selector('button:has-text("candidature spontanée")')
            if not btn:
                self._log("  ⚠️  Bouton postuler introuvable", "warning")
                return

            await btn.click()
            await asyncio.sleep(2)

            ok = await self._fill_form(page, company_name)
            if ok:
                self._record_sent(siret, company_name, city_name, job_name)
                self.stats["sent_today"] += 1
                self._update_stats(sent_today=self.stats["sent_today"])
                self._log(f"  ✅ Candidature envoyée: {company_name}", "success")
            else:
                self.stats["errors"] += 1
                self._update_stats(errors=self.stats["errors"])
                self._log(f"  ⚠️  Échec: {company_name}", "warning")

        except Exception as exc:
            self.stats["errors"] += 1
            self._update_stats(errors=self.stats["errors"])
            self._log(f"  ❌ Erreur: {exc}", "error")

    async def _fill_form(self, page, company_name: str) -> bool:
        from playwright.async_api import TimeoutError as PwTimeout

        cfg = self.config

        # Wait for the candidature form to appear
        try:
            await page.wait_for_selector(
                '[data-testid="CandidatureSpontaneeTitle"], h1:has-text("Candidature spontanée")',
                timeout=10_000,
            )
        except PwTimeout:
            self._log("  ⏱️  Formulaire non visible", "warning")
            return False

        try:
            # ---------- 1. Cocher les 3 cases de préparation ----------
            labels = await page.query_selector_all(
                ".checkbox-container label, .checkbox-container .MuiFormControlLabel-root"
            )
            if not labels:
                # Fallback: find all checkboxes in the form
                checkboxes = await page.query_selector_all(
                    'form input[type="checkbox"]'
                )
                for cb in checkboxes:
                    if not await cb.is_checked():
                        await cb.click(force=True)
                        await asyncio.sleep(0.3)
            else:
                for label in labels:
                    await label.click()
                    await asyncio.sleep(0.3)

            # ---------- 2. Message (facultatif) ----------
            template = cfg.get("message_template", "")
            if template:
                msg = (
                    template
                    .replace("{company}", company_name)
                    .replace("{firstname}", cfg.get("firstname", ""))
                    .replace("{lastname}", cfg.get("lastname", ""))
                )
                ta = await page.query_selector(
                    '[data-testid="message"], #message, textarea[name="applicant_message"]'
                )
                if ta:
                    await ta.fill(msg)
                    await asyncio.sleep(0.4)

            # ---------- 3. Champs personnels ----------
            field_map = [
                ('#lastName,  [name="applicant_last_name"]',  "lastname"),
                ('#firstName, [name="applicant_first_name"]', "firstname"),
                ('#email,     [name="applicant_email"]',      "email"),
                ('#phone,     [name="applicant_phone"]',      "phone"),
            ]
            for selector_group, key in field_map:
                value = cfg.get(key, "")
                if not value:
                    continue
                for sel in selector_group.split(","):
                    sel = sel.strip()
                    try:
                        field = await page.query_selector(sel)
                        if field:
                            await field.fill(value)
                            await asyncio.sleep(0.2)
                            break
                    except Exception:
                        pass

            # ---------- 4. Upload CV ----------
            cv_path = cfg.get("cv_path", "")
            if cv_path and Path(cv_path).exists():
                fi = await page.query_selector('input[type="file"]')
                if fi:
                    await fi.set_input_files(cv_path)
                    await asyncio.sleep(2)
                    self._log("  📄 CV chargé")
            else:
                self._log("  ⚠️  CV non configuré ou introuvable", "warning")

            # ---------- 5. Soumettre ----------
            submit = await page.query_selector(
                '[data-testid="candidature-not-sent"], button[type="submit"]:has-text("J\'envoie")'
            )
            if not submit:
                self._log("  ❌ Bouton soumettre introuvable", "error")
                return False

            await submit.click()
            await asyncio.sleep(5)

            # ---------- 6. Vérifier le succès ----------
            success_el = await page.query_selector('[data-testid="application-success"]')
            if success_el:
                return True

            body = await page.content()
            return "a bien été envoyée" in body or "application-success" in body

        except Exception as exc:
            self._log(f"  ❌ Formulaire: {exc}", "error")
            return False

    def stop(self) -> None:
        self.stop_event.set()
