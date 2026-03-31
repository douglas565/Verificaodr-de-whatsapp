"""
WhatsApp Group Monitor — Versão 4 (Histórico Completo)
=======================================================
CORREÇÃO PRINCIPAL: Extração de histórico completo via rolagem incremental.

Problema raiz das versões anteriores:
  - WhatsApp Web usa DOM virtualizado: só mantém ~50-80 bolhas por vez.
  - scrollTop = 0 ou PAGE_UP pula direto ao topo sem o WA ter tempo de
    carregar os lotes intermediários → mensagens do meio se perdem.

Solução aplicada nesta versão:
  1. Rolagem INCREMENTAL por passos fixos (ex: 400px por vez), esperando
     o DOM crescer entre cada passo antes de continuar.
  2. MutationObserver via JS para detectar inserção de novos nós com
     precisão, sem depender de sleep fixo.
  3. Fallback triplo de rolagem: scrollTop decremental → scrollBy → PAGE_UP.
  4. Tolerância de 15 ciclos sem novidade antes de declarar início da conversa.
  5. Varredura final ao atingir scrollTop == 0 para capturar o último lote.
  6. Todos os métodos da classe App completos e sem duplicação.

Dependências:
    pip install selenium webdriver-manager customtkinter pillow

Chrome deve estar instalado no sistema.
"""

import os
import time
import base64
import hashlib
import threading
import sqlite3
from datetime import datetime

import customtkinter as ctk
from tkinter import messagebox, filedialog

try:
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.common.action_chains import ActionChains
    from selenium.common.exceptions import (
        TimeoutException, StaleElementReferenceException
    )
    from webdriver_manager.chrome import ChromeDriverManager
    SELENIUM_OK = True
except ImportError:
    SELENIUM_OK = False


# ══════════════════════════════════════════════════════════════════════
# MOTOR DE CAPTURA
# ══════════════════════════════════════════════════════════════════════

class MotorWhatsApp:
    """Gerencia conexão e captura de mensagens via WhatsApp Web."""

    # ── Script JS: espera o DOM crescer usando MutationObserver ────────
    _JS_AGUARDAR_NOVAS = """
        (function(container, qtdAntes, timeout, callback) {
            var inicio = Date.now();
            var obs = new MutationObserver(function() {
                var bolhas = container.querySelectorAll(
                    'div.message-in, div.message-out'
                );
                if (bolhas.length > qtdAntes) {
                    obs.disconnect();
                    callback(true);
                } else if (Date.now() - inicio > timeout) {
                    obs.disconnect();
                    callback(false);
                }
            });
            obs.observe(container, { childList: true, subtree: true });
            // Timeout de segurança caso o observer nunca dispare
            setTimeout(function() {
                obs.disconnect();
                var bolhas = container.querySelectorAll(
                    'div.message-in, div.message-out'
                );
                callback(bolhas.length > qtdAntes);
            }, timeout);
        })(arguments[0], arguments[1], arguments[2], arguments[3]);
    """

    def __init__(self, diretorio="dados_whatsapp", callback_log=None, callback_msg=None):
        self.diretorio    = diretorio
        self.callback_log = callback_log or print
        self.callback_msg = callback_msg
        self.driver       = None
        self.monitorando  = False
        self.grupo_atual  = None
        self._criar_dirs()
        self._init_db()

    # ── Setup ──────────────────────────────────────────────────────────
    def _criar_dirs(self):
        for sub in ["fotos", "logs", "videos", "db", "exports"]:
            os.makedirs(os.path.join(self.diretorio, sub), exist_ok=True)

    def _init_db(self):
        db_path = os.path.join(self.diretorio, "db", "mensagens.db")
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS mensagens (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                grupo     TEXT,
                autor     TEXT,
                conteudo  TEXT,
                tipo      TEXT DEFAULT 'texto',
                foto_path TEXT,
                timestamp TEXT,
                hash_msg  TEXT UNIQUE
            )
        """)
        for col, tipo in [("foto_path", "TEXT"), ("hash_msg", "TEXT")]:
            try:
                self.conn.execute(f"ALTER TABLE mensagens ADD COLUMN {col} {tipo}")
            except Exception:
                pass
        self.conn.commit()

    # ── Chrome ─────────────────────────────────────────────────────────
    def iniciar_chrome(self):
        self.callback_log("🚀 Iniciando Chrome...")
        opts = Options()
        opts.add_argument("--start-maximized")
        opts.add_argument("--disable-notifications")
        opts.add_argument("--disable-popup-blocking")
        perfil = os.path.join(os.getcwd(), "whatsapp_session")
        opts.add_argument(f"--user-data-dir={perfil}")
        opts.add_experimental_option("excludeSwitches", ["enable-automation"])
        opts.add_experimental_option("useAutomationExtension", False)
        service = Service(ChromeDriverManager().install())
        self.driver = webdriver.Chrome(service=service, options=opts)
        self.driver.get("https://web.whatsapp.com")
        self.callback_log("✅ Chrome aberto! Escaneie o QR Code no navegador.")
        return True

    def aguardar_login(self, timeout=120):
        self.callback_log("⏳ Aguardando login... (máx 2 min)")
        try:
            WebDriverWait(self.driver, timeout).until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR,
                     'div[data-testid="default-user"],'
                     'div[data-testid="chat-list"],'
                     '#side')
                )
            )
            self.callback_log("🎉 Login realizado com sucesso!")
            return True
        except TimeoutException:
            self.callback_log("❌ Tempo esgotado. Tente novamente.")
            return False

    # ── Grupos ─────────────────────────────────────────────────────────
    def listar_grupos(self):
        grupos = []
        try:
            self.callback_log("🔍 Vasculhando interface do WhatsApp...")
            time.sleep(5)
            for seletor in [
                'span[data-testid="cell-frame-title"]',
                'div._ak8q',
                'span.selectable-text.copyable-text',
                'div[dir="auto"]',
            ]:
                for el in self.driver.find_elements(By.CSS_SELECTOR, seletor):
                    try:
                        nome = el.text.strip()
                        if len(nome) > 1 and nome not in grupos and "\n" not in nome:
                            grupos.append(nome)
                    except Exception:
                        continue

            if not grupos:
                self.callback_log("⚠️ Tentando busca profunda via XPath...")
                for el in self.driver.find_elements(By.XPATH, '//span[@dir="auto"]'):
                    nome = el.text.strip()
                    if len(nome) > 2 and nome not in grupos:
                        grupos.append(nome)

            lixo = {"WhatsApp", "Conversas", "Status", "Canais", "Comunidades", "Novo chat"}
            grupos = [g for g in grupos if g not in lixo]
            self.callback_log(f"✅ {len(grupos)} chats encontrados.")
            return sorted(list(set(grupos)))
        except Exception as e:
            self.callback_log(f"⚠️ Erro na listagem: {e}")
            return []

    def abrir_grupo(self, nome_grupo):
        try:
            self.callback_log(f"🔍 Abrindo: {nome_grupo}")
            for chat in self.driver.find_elements(By.CSS_SELECTOR, "span[title]"):
                if chat.get_attribute("title") == nome_grupo:
                    self.driver.execute_script("arguments[0].click();", chat)
                    self.grupo_atual = nome_grupo
                    time.sleep(2)
                    return True

            sb = WebDriverWait(self.driver, 10).until(
                EC.element_to_be_clickable(
                    (By.CSS_SELECTOR, 'div[contenteditable="true"][data-tab="3"]')
                )
            )
            sb.click()
            sb.clear()
            for letra in nome_grupo:
                sb.send_keys(letra)
                time.sleep(0.05)
            time.sleep(2)

            resultado = WebDriverWait(self.driver, 10).until(
                EC.element_to_be_clickable(
                    (By.XPATH, f"//span[@title='{nome_grupo}']")
                )
            )
            self.driver.execute_script("arguments[0].click();", resultado)
            self.grupo_atual = nome_grupo
            self.callback_log(f"✅ '{nome_grupo}' aberto.")
            time.sleep(2)
            return True
        except Exception as e:
            self.callback_log(f"❌ Erro ao abrir grupo: {e}")
            return False

    # ── Extração de atributos de uma bolha ─────────────────────────────
    def _extrair_texto_bolha(self, el):
        # 1) innerText do container .copyable-text
        try:
            c = el.find_element(By.CSS_SELECTOR, ".copyable-text")
            t = (c.get_attribute("innerText") or "").strip()
            if t:
                return t
        except Exception:
            pass
        # 2) spans selecionáveis
        try:
            partes = [
                (s.get_attribute("innerText") or s.text or "").strip()
                for s in el.find_elements(By.CSS_SELECTOR, "span.selectable-text")
            ]
            t = " ".join(p for p in partes if p)
            if t:
                return t
        except Exception:
            pass
        # 3) qualquer span com dir
        try:
            vistos = []
            for s in el.find_elements(By.CSS_SELECTOR, "span[dir]"):
                t = (s.get_attribute("innerText") or s.text or "").strip()
                if t and t not in vistos:
                    vistos.append(t)
            t = " ".join(vistos)
            if t:
                return t
        except Exception:
            pass
        # 4) innerText bruto (filtra linhas de horário)
        try:
            linhas = [
                l for l in (el.get_attribute("innerText") or "").splitlines()
                if l.strip() and not l.strip().replace(":", "").isdigit()
            ]
            return " ".join(linhas).strip()
        except Exception:
            pass
        return ""

    def _extrair_timestamp_bolha(self, el):
        try:
            c = el.find_element(By.CSS_SELECTOR, ".copyable-text")
            meta = c.get_attribute("data-pre-plain-text") or ""
            if "[" in meta:
                return meta.split("]")[0].replace("[", "").strip()
        except Exception:
            pass
        try:
            s = el.find_element(
                By.CSS_SELECTOR,
                'span[data-testid="msg-meta"] span, span[aria-label]'
            )
            lbl = s.get_attribute("aria-label") or s.text
            if lbl:
                return lbl.strip()
        except Exception:
            pass
        return datetime.now().strftime("%H:%M, %d/%m/%Y")

    def _extrair_autor_bolha(self, el):
        try:
            if "message-out" in (el.get_attribute("class") or ""):
                return "Você"
        except Exception:
            pass
        for sel in ['span[data-testid="author"]', 'span.copyable-text[dir="auto"]', "._akbu"]:
            try:
                nome = (el.find_element(By.CSS_SELECTOR, sel).text or "").strip()
                if nome:
                    return nome
            except Exception:
                pass
        try:
            c = el.find_element(By.CSS_SELECTOR, ".copyable-text")
            meta = c.get_attribute("data-pre-plain-text") or ""
            if "] " in meta:
                return meta.split("] ", 1)[1].rstrip(":")
        except Exception:
            pass
        return "Desconhecido"

    def _baixar_imagem_blob(self, src_url, nome_img):
        path_img = os.path.join(self.diretorio, "fotos", nome_img)
        script = """
            var uri = arguments[0], cb = arguments[1];
            var xhr = new XMLHttpRequest();
            xhr.responseType = 'blob';
            xhr.onload = function() {
                var r = new FileReader();
                r.onloadend = function() { cb(r.result); };
                r.readAsDataURL(xhr.response);
            };
            xhr.onerror = function() { cb(''); };
            xhr.open('GET', uri); xhr.send();
        """
        try:
            b64 = self.driver.execute_async_script(script, src_url)
            if b64 and "," in b64:
                with open(path_img, "wb") as f:
                    f.write(base64.b64decode(b64.split(",")[1]))
                return path_img
        except Exception:
            pass
        return None

    # ── Captura de mensagens no DOM ────────────────────────────────────
    def capturar_mensagens_visiveis(self):
        mensagens = []
        try:
            for el in self.driver.find_elements(
                By.CSS_SELECTOR, "div.message-in, div.message-out"
            ):
                try:
                    texto     = self._extrair_texto_bolha(el)
                    autor     = self._extrair_autor_bolha(el)
                    timestamp = self._extrair_timestamp_bolha(el)
                    foto_path = None
                    tipo      = "texto"

                    try:
                        img = el.find_element(By.CSS_SELECTOR, 'img[src*="blob:"]')
                        src = img.get_attribute("src")
                        if src:
                            nome = f"img_{hashlib.md5(src.encode()).hexdigest()[:10]}.jpg"
                            foto_path = self._baixar_imagem_blob(src, nome)
                            tipo  = "imagem" if foto_path else tipo
                            texto = f"{texto} [IMAGEM]" if texto else "[IMAGEM]"
                    except Exception:
                        pass

                    if not texto.strip() and not foto_path:
                        continue

                    chave = f"{autor}|{texto}|{timestamp}|{self.grupo_atual}"
                    mensagens.append({
                        "autor":     autor,
                        "texto":     texto,
                        "timestamp": timestamp,
                        "grupo":     self.grupo_atual or "Desconhecido",
                        "tipo":      tipo,
                        "foto_path": foto_path,
                        "hash_msg":  hashlib.md5(chave.encode()).hexdigest(),
                    })
                except StaleElementReferenceException:
                    continue
                except Exception:
                    continue
        except Exception as e:
            self.callback_log(f"⚠️ Erro na captura: {e}")
        return mensagens

    # ── Utilitários de scroll ──────────────────────────────────────────
    def _encontrar_container_msgs(self):
        for sel in [
            'div[data-testid="conversation-panel-messages"]',
            "#main div[role=\"application\"]",
            "#main div.copyable-area",
            "#main",
        ]:
            try:
                el = self.driver.find_element(By.CSS_SELECTOR, sel)
                if el:
                    return el
            except Exception:
                continue
        return None

    def _contar_bolhas(self):
        try:
            return len(self.driver.find_elements(
                By.CSS_SELECTOR, "div.message-in, div.message-out"
            ))
        except Exception:
            return 0

    def _scroll_top_atual(self, container):
        try:
            return self.driver.execute_script(
                "return arguments[0].scrollTop;", container
            )
        except Exception:
            return -1

    def _rolar_passo(self, container, passo=400):
        """
        Rola para cima em pequenos passos para ativar o lazy-load do WhatsApp.
        Usa 3 estratégias em cascata.
        """
        # Estratégia 1 — scrollTop decremental (mais confiável no WA)
        try:
            atual = self.driver.execute_script(
                "return arguments[0].scrollTop;", container
            )
            novo = max(0, atual - passo)
            self.driver.execute_script(
                "arguments[0].scrollTop = arguments[1];", container, novo
            )
            return
        except Exception:
            pass

        # Estratégia 2 — scrollBy nativo do browser
        try:
            self.driver.execute_script(
                "arguments[0].scrollBy(0, arguments[1]);", container, -passo
            )
            return
        except Exception:
            pass

        # Estratégia 3 — PAGE_UP via ActionChains
        try:
            ActionChains(self.driver).move_to_element(container)\
                .send_keys(Keys.PAGE_UP).perform()
        except Exception:
            pass

    def _aguardar_dom_crescer(self, container, qtd_antes, timeout_ms=9000):
        """
        Usa MutationObserver (JS) para detectar quando o WA insere
        novas bolhas no DOM. Muito mais preciso do que sleep fixo.
        Retorna True se novas mensagens apareceram.
        """
        try:
            resultado = self.driver.execute_async_script(
                self._JS_AGUARDAR_NOVAS,
                container,
                qtd_antes,
                timeout_ms,
            )
            return bool(resultado)
        except Exception:
            # Fallback: polling simples
            fim = time.time() + timeout_ms / 1000
            while time.time() < fim:
                if self._contar_bolhas() > qtd_antes:
                    return True
                time.sleep(0.4)
            return False

    # ── Extração Completa do Histórico (método principal) ───────────────
    def extrair_historico_completo(self, grupo, qtd_passos=300, passo_px=400):
        """
        Extrai TODAS as mensagens de um grupo rolando para cima
        em passos pequenos e esperando o DOM carregar a cada passo.

        Parâmetros
        ----------
        qtd_passos : int
            Número máximo de passos de rolagem. Cada passo ≈ 400 px ≈ 5–15 msgs.
            300 passos cobrem conversas muito longas (anos de histórico).
        passo_px : int
            Pixels deslocados por passo. 400 px é o ponto ideal:
            grande o suficiente para avançar, pequeno o suficiente para
            o WA carregar sem perder mensagens intermediárias.
        """
        if not self.abrir_grupo(grupo):
            return []

        self.callback_log(f"📜 Iniciando extração completa de '{grupo}'...")
        self.callback_log(
            f"   Estratégia: {qtd_passos} passos × {passo_px}px "
            f"(≈ {qtd_passos * 10}–{qtd_passos * 20} mensagens)"
        )

        todas       = {}   # hash → msg
        sem_novo    = 0    # ciclos consecutivos sem crescimento do DOM
        MAX_SEM_NOVO = 15  # tolerância alta para conexões lentas

        container = self._encontrar_container_msgs()
        if not container:
            self.callback_log("❌ Container de mensagens não encontrado.")
            return []

        # Garante que o WA renderizou as mensagens mais recentes
        try:
            self.driver.execute_script(
                "arguments[0].scrollTop = arguments[0].scrollHeight;", container
            )
            time.sleep(2.5)
        except Exception:
            pass

        passo_atual = 0
        while passo_atual < qtd_passos:
            qtd_dom_antes = self._contar_bolhas()

            # ── Captura snapshot do que está no DOM agora ──
            novas_ciclo = 0
            for m in self.capturar_mensagens_visiveis():
                h = m["hash_msg"]
                if h not in todas:
                    todas[h] = m
                    self._salvar_mensagem(m)
                    novas_ciclo += 1

            # ── Verifica se chegamos ao início absoluto ────
            scroll_pos = self._scroll_top_atual(container)
            if scroll_pos == 0:
                # Está no topo — faz uma última captura e encerra
                self.callback_log("📌 scrollTop == 0: topo da conversa alcançado!")
                time.sleep(2)
                for m in self.capturar_mensagens_visiveis():
                    h = m["hash_msg"]
                    if h not in todas:
                        todas[h] = m
                        self._salvar_mensagem(m)
                break

            # ── Rola um passo para cima ────────────────────
            self._rolar_passo(container, passo_px)
            passo_atual += 1

            # ── Aguarda o DOM crescer (MutationObserver) ──
            dom_cresceu = self._aguardar_dom_crescer(
                container, qtd_dom_antes, timeout_ms=9000
            )

            if dom_cresceu:
                sem_novo = 0
                if passo_atual % 10 == 0 or novas_ciclo > 0:
                    self.callback_log(
                        f"⏳ [{passo_atual}/{qtd_passos}] "
                        f"+{novas_ciclo} novas | Total: {len(todas)}"
                    )
            else:
                sem_novo += 1
                self.callback_log(
                    f"⏳ [{passo_atual}/{qtd_passos}] DOM estável "
                    f"({sem_novo}/{MAX_SEM_NOVO}) | Total: {len(todas)}"
                )

                if sem_novo >= MAX_SEM_NOVO:
                    # Tenta forçar o topo absoluto antes de desistir
                    self.callback_log("🔄 Tentando forçar scrollTop = 0...")
                    try:
                        self.driver.execute_script(
                            "arguments[0].scrollTop = 0;", container
                        )
                        time.sleep(4)
                        qtd_depois = self._contar_bolhas()
                        if qtd_depois > qtd_dom_antes:
                            sem_novo = 0
                            self.callback_log("✅ Novo lote carregado após forçar topo!")
                            continue
                    except Exception:
                        pass
                    self.callback_log("📌 Início da conversa atingido!")
                    break

        # Captura final (garante o último lote visível)
        for m in self.capturar_mensagens_visiveis():
            h = m["hash_msg"]
            if h not in todas:
                todas[h] = m
                self._salvar_mensagem(m)

        resultado = list(todas.values())
        self.callback_log(f"🏁 Extração concluída! {len(resultado)} mensagens únicas.")
        return resultado

    # ── Monitoramento em Tempo Real ────────────────────────────────────
    def iniciar_monitoramento(self, grupo):
        self.monitorando = True
        self.abrir_grupo(grupo)
        self.callback_log(f"🎧 Monitoramento em tempo real: '{grupo}'")
        vistas = set()
        while self.monitorando:
            try:
                for msg in self.capturar_mensagens_visiveis():
                    h = msg["hash_msg"]
                    if h not in vistas:
                        vistas.add(h)
                        self._salvar_mensagem(msg)
                        if self.callback_msg:
                            self.callback_msg(msg)
                time.sleep(3)
            except Exception as e:
                self.callback_log(f"⚠️ Erro no monitor: {e}")
                time.sleep(5)

    def parar_monitoramento(self):
        self.monitorando = False
        self.callback_log("⏹️ Monitoramento pausado.")

    # ── Persistência ────────────────────────────────────────────────────
    def _salvar_mensagem(self, msg):
        try:
            self.conn.execute(
                """INSERT OR IGNORE INTO mensagens
                   (grupo, autor, conteudo, tipo, foto_path, timestamp, hash_msg)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    msg["grupo"], msg["autor"], msg["texto"],
                    msg.get("tipo", "texto"), msg.get("foto_path"),
                    msg["timestamp"], msg["hash_msg"],
                ),
            )
            self.conn.commit()
        except Exception:
            pass

    def buscar_mensagens_db(self, grupo=None, limite=500):
        q = "SELECT grupo, autor, conteudo, timestamp FROM mensagens"
        p = []
        if grupo:
            q += " WHERE grupo LIKE ?"
            p.append(f"%{grupo}%")
        q += " ORDER BY id DESC LIMIT ?"
        p.append(limite)
        return self.conn.execute(q, p).fetchall()

    # ── Exportação HTML ────────────────────────────────────────────────
    def exportar_html_combinado(self, grupo, msgs):
        if not msgs:
            return None
        nome    = grupo.replace("/", "-").replace("\\", "-")[:50]
        ts      = datetime.now().strftime("%Y%m%d_%H%M%S")
        caminho = os.path.join(self.diretorio, "exports", f"{nome}_{ts}.html")

        def b64(path):
            try:
                if path and os.path.exists(path):
                    return base64.b64encode(open(path, "rb").read()).decode()
            except Exception:
                pass
            return None

        bolhas = []
        for m in msgs:
            autor   = m["autor"]
            texto   = m["texto"].replace("[IMAGEM]", "").strip()
            ts_msg  = m["timestamp"]
            is_out  = autor == "Você"
            lado    = "msg-out" if is_out else "msg-in"
            img_b64 = b64(m.get("foto_path"))
            partes  = []
            if img_b64:
                partes.append(
                    f'<img src="data:image/jpeg;base64,{img_b64}" '
                    f'style="max-width:300px;border-radius:8px;'
                    f'display:block;margin-bottom:4px;"/>'
                )
            if texto:
                partes.append(f'<span class="msg-text">{texto}</span>')
            if not partes:
                continue
            bolhas.append(f"""
            <div class="msg-wrapper {lado}">
              <div class="bubble">
                {"" if is_out else f'<span class="autor">{autor}</span>'}
                {"".join(partes)}
                <span class="ts">{ts_msg}</span>
              </div>
            </div>""")

        html = f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8">
  <title>Histórico — {grupo}</title>
  <style>
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
          background:#e5ddd5;padding:20px}}
    h1{{text-align:center;color:#075e54;padding:16px;background:#fff;
        border-radius:12px;margin-bottom:20px;font-size:18px}}
    .info-bar{{text-align:center;color:#555;font-size:12px;background:#fff;
               border-radius:8px;padding:6px 12px;margin:8px auto;max-width:380px}}
    .chat-container{{max-width:720px;margin:0 auto;
                     display:flex;flex-direction:column;gap:4px}}
    .msg-wrapper{{display:flex;margin:2px 0}}
    .msg-in{{justify-content:flex-start}}
    .msg-out{{justify-content:flex-end}}
    .bubble{{max-width:72%;padding:8px 12px;border-radius:12px;
             font-size:14px;line-height:1.4;
             box-shadow:0 1px 2px rgba(0,0,0,.15)}}
    .msg-in  .bubble{{background:#fff;border-top-left-radius:0}}
    .msg-out .bubble{{background:#dcf8c6;border-top-right-radius:0}}
    .autor{{display:block;font-weight:700;font-size:13px;
            color:#128c7e;margin-bottom:3px}}
    .msg-text{{display:block;word-break:break-word}}
    .ts{{display:block;font-size:11px;color:#999;
         text-align:right;margin-top:4px}}
  </style>
</head>
<body>
  <h1>📱 {grupo}</h1>
  <div class="info-bar">
    {len(msgs)} mensagens · exportado em
    {datetime.now().strftime("%d/%m/%Y %H:%M")}
  </div>
  <div class="chat-container">{"".join(bolhas)}</div>
</body>
</html>"""
        with open(caminho, "w", encoding="utf-8") as f:
            f.write(html)
        self.callback_log(f"📄 HTML exportado: {caminho}")
        return caminho

    def _exportar_txt(self, grupo, msgs):
        nome = grupo.replace("/", "-").replace("\\", "-")[:50]
        path = os.path.join(self.diretorio, "logs", f"{nome}.txt")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"\n--- {datetime.now().strftime('%Y-%m-%d %H:%M')} ---\n")
            for m in msgs:
                foto = f" [foto: {m['foto_path']}]" if m.get("foto_path") else ""
                f.write(f"[{m['timestamp']}] {m['autor']}: {m['texto']}{foto}\n")
        self.callback_log(f"💾 TXT salvo: {path}")
        return path

    def fechar(self):
        self.monitorando = False
        try:
            if self.driver:
                self.driver.quit()
        except Exception:
            pass
        try:
            if self.conn:
                self.conn.close()
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════
# INTERFACE GRÁFICA
# ══════════════════════════════════════════════════════════════════════

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

VERDE    = "#00C896"
VERMELHO = "#FF4757"
AMARELO  = "#FFA502"
CINZA    = "#2d2d2d"
FUNDO    = "#1a1a2e"


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("WhatsApp Monitor Pro — v4")
        self.geometry("1100x720")
        self.configure(fg_color=FUNDO)
        self.protocol("WM_DELETE_WINDOW", self._ao_fechar)

        self.motor           = None
        self.lista_grupos    = []
        self._thread_bot     = None
        self._thread_mon     = None
        self._msgs_extraidas = []

        self._build_ui()

        if not SELENIUM_OK:
            self._log("❌ Selenium não instalado!")
            self._log("   Execute: pip install selenium webdriver-manager")

    # ── UI ─────────────────────────────────────────────────────────────
    def _build_ui(self):
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self._build_sidebar()
        self._build_main()

    def _build_sidebar(self):
        sb = ctk.CTkFrame(self, width=220, corner_radius=0, fg_color="#16213e")
        sb.grid(row=0, column=0, sticky="nsew")
        sb.grid_propagate(False)

        ctk.CTkLabel(sb, text="📱 WA Monitor",
                     font=ctk.CTkFont("Helvetica", 20, "bold"),
                     text_color=VERDE).pack(pady=(30, 5))
        ctk.CTkLabel(sb, text="Pro Edition v4",
                     font=ctk.CTkFont("Helvetica", 11),
                     text_color="#888").pack(pady=(0, 25))

        self.dot = ctk.CTkLabel(sb, text="● Desconectado",
                                text_color=VERMELHO,
                                font=ctk.CTkFont("Helvetica", 12))
        self.dot.pack(pady=5)

        ctk.CTkFrame(sb, height=2, fg_color="gray30").pack(fill="x", padx=20, pady=12)

        for texto, cmd in [
            ("📁  Abrir Fotos",   self._abrir_fotos),
            ("📄  Abrir Exports", self._abrir_exports),
            ("📝  Abrir Logs",    self._abrir_logs),
            ("🗃️  Ver Banco",     self._ver_banco),
        ]:
            ctk.CTkButton(sb, text=texto, command=cmd,
                          fg_color="transparent", hover_color="#1e3a5f",
                          anchor="w", font=ctk.CTkFont("Helvetica", 13)
                          ).pack(fill="x", padx=15, pady=3)

        ctk.CTkFrame(sb, height=2, fg_color="gray30").pack(fill="x", padx=20, pady=12)

        ctk.CTkLabel(sb, text="Tema", text_color="#888",
                     font=ctk.CTkFont("Helvetica", 11)).pack()
        self.tema_switch = ctk.CTkSwitch(sb, text="Modo Claro",
                                         command=self._toggle_tema,
                                         onvalue="Light", offvalue="Dark")
        self.tema_switch.pack(pady=5)

    def _build_main(self):
        main = ctk.CTkFrame(self, corner_radius=12, fg_color=CINZA)
        main.grid(row=0, column=1, padx=15, pady=15, sticky="nsew")
        main.grid_columnconfigure(0, weight=1)
        main.grid_rowconfigure(0, weight=1)

        self.tabs = ctk.CTkTabview(
            main, fg_color="#222",
            segmented_button_selected_color=VERDE,
            segmented_button_selected_hover_color="#00a67c",
        )
        self.tabs.grid(row=0, column=0, padx=10, pady=10, sticky="nsew")
        for aba in ["🔗  Conexão", "📜  Histórico", "📡  Tempo Real", "🔍  Busca"]:
            self.tabs.add(aba)

        self._build_tab_conexao()
        self._build_tab_historico()
        self._build_tab_tempo_real()
        self._build_tab_busca()

    # ── Tab Conexão ────────────────────────────────────────────────────
    def _build_tab_conexao(self):
        tab = self.tabs.tab("🔗  Conexão")
        tab.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(tab, text="Conectar ao WhatsApp Web",
                     font=ctk.CTkFont("Helvetica", 18, "bold")).pack(pady=(20, 5))
        ctk.CTkLabel(tab,
                     text="O Chrome abrirá automaticamente. Escaneie o QR Code com seu celular.",
                     text_color="#aaa", wraplength=500).pack(pady=(0, 20))

        card = ctk.CTkFrame(tab, fg_color="#1a1a2e", corner_radius=10)
        card.pack(padx=40, pady=10, fill="x")
        for emoji, texto in [
            ("1️⃣", "Clique em 'Iniciar Conexão'"),
            ("2️⃣", "O Chrome vai abrir com o WhatsApp Web"),
            ("3️⃣", "No celular: WhatsApp → Aparelhos Conectados → Conectar"),
            ("4️⃣", "Escaneie o QR Code na tela do Chrome"),
            ("5️⃣", "Aguarde o status mudar para CONECTADO"),
        ]:
            f = ctk.CTkFrame(card, fg_color="transparent")
            f.pack(fill="x", padx=15, pady=3)
            ctk.CTkLabel(f, text=emoji, width=30).pack(side="left")
            ctk.CTkLabel(f, text=texto, anchor="w", text_color="#ccc").pack(side="left")

        self.btn_conectar = ctk.CTkButton(
            tab, text="🚀  Iniciar Conexão",
            command=self._iniciar_conexao,
            font=ctk.CTkFont("Helvetica", 14, "bold"),
            height=45, fg_color=VERDE, hover_color="#00a67c", text_color="black",
        )
        self.btn_conectar.pack(pady=20, padx=40, fill="x")

        ctk.CTkButton(
            tab, text="⏹  Desconectar",
            command=self._desconectar,
            height=35, fg_color="transparent",
            border_color=VERMELHO, border_width=1,
            text_color=VERMELHO, hover_color="#3d1a1a",
        ).pack(pady=5, padx=40, fill="x")

        ctk.CTkLabel(tab, text="Log do Sistema:", anchor="w").pack(padx=40, fill="x")
        self.log_conexao = ctk.CTkTextbox(tab, height=140,
                                          font=ctk.CTkFont("Courier", 11))
        self.log_conexao.pack(padx=40, pady=(5, 20), fill="x")

    # ── Tab Histórico ──────────────────────────────────────────────────
    def _build_tab_historico(self):
        tab = self.tabs.tab("📜  Histórico")
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(7, weight=1)

        ctk.CTkLabel(tab, text="Extrair Histórico Completo",
                     font=ctk.CTkFont("Helvetica", 18, "bold")).pack(pady=(20, 5))
        ctk.CTkLabel(
            tab,
            text="Rola em passos pequenos e espera o WA carregar cada lote.\n"
                 "Mais passos = histórico mais antigo recuperado.",
            text_color="#aaa", justify="center",
        ).pack(pady=(0, 10))

        ctk.CTkLabel(tab, text="Grupo:", anchor="w").pack(padx=30, fill="x")
        self.combo_hist = ctk.CTkComboBox(
            tab, values=["⏳ Conecte-se primeiro..."], height=35)
        self.combo_hist.pack(padx=30, pady=5, fill="x")

        # ── Slider de passos ──────────────────────
        frm = ctk.CTkFrame(tab, fg_color="transparent")
        frm.pack(fill="x", padx=30)
        ctk.CTkLabel(frm,
                     text="Nº de passos de rolagem (400px/passo ≈ 10–20 msgs):",
                     anchor="w").pack(fill="x")
        self.slider_hist = ctk.CTkSlider(frm, from_=50, to=1000,
                                         number_of_steps=95)
        self.slider_hist.set(300)
        self.slider_hist.pack(fill="x")
        self.slider_hist.configure(command=self._update_slider_label)
        self.label_slider = ctk.CTkLabel(frm, text="300 passos ≈ ~3.000–6.000 msgs")
        self.label_slider.pack()

        self.label_tempo = ctk.CTkLabel(
            tab,
            text="⏱ Tempo estimado: ~45 min  (varia com tamanho do grupo e rede)",
            text_color=AMARELO, font=ctk.CTkFont("Helvetica", 11),
        )
        self.label_tempo.pack()

        # ── Botões ────────────────────────────────
        bf = ctk.CTkFrame(tab, fg_color="transparent")
        bf.pack(fill="x", padx=30, pady=8)

        self.btn_extrair = ctk.CTkButton(
            bf, text="📥  Extrair Histórico Completo",
            command=self._extrair_historico,
            height=42, fg_color=VERDE, hover_color="#00a67c",
            text_color="black", state="disabled",
        )
        self.btn_extrair.pack(side="left", fill="x", expand=True, padx=(0, 5))

        self.btn_export_html = ctk.CTkButton(
            bf, text="🌐  Salvar HTML",
            command=self._salvar_html_combinado,
            height=42, fg_color="#1565c0", hover_color="#0d47a1",
            state="disabled",
        )
        self.btn_export_html.pack(side="left", fill="x", expand=True, padx=(5, 0))

        self.btn_salvar_txt = ctk.CTkButton(
            tab, text="💾  Salvar TXT",
            command=self._salvar_historico_txt,
            height=32, fg_color="#333", hover_color="#444",
        )
        self.btn_salvar_txt.pack(fill="x", padx=30, pady=(0, 5))

        self.progress_bar = ctk.CTkProgressBar(tab)
        self.progress_bar.pack(fill="x", padx=30, pady=(0, 5))
        self.progress_bar.set(0)

        ctk.CTkLabel(tab, text="Mensagens extraídas:", anchor="w").pack(padx=30, fill="x")
        self.txt_historico = ctk.CTkTextbox(tab, font=ctk.CTkFont("Courier", 11))
        self.txt_historico.pack(padx=30, pady=5, fill="both", expand=True)

    # ── Tab Tempo Real ─────────────────────────────────────────────────
    def _build_tab_tempo_real(self):
        tab = self.tabs.tab("📡  Tempo Real")
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(2, weight=1)

        ctk.CTkLabel(tab, text="Monitoramento em Tempo Real",
                     font=ctk.CTkFont("Helvetica", 18, "bold")).pack(pady=(20, 5))
        ctk.CTkLabel(tab,
                     text="Mensagens novas aparecem aqui automaticamente (a cada 3s)",
                     text_color="#aaa").pack(pady=(0, 10))

        ctrl = ctk.CTkFrame(tab, fg_color="transparent")
        ctrl.pack(fill="x", padx=30)
        ctk.CTkLabel(ctrl, text="Grupo:", width=60).pack(side="left")
        self.combo_monitor = ctk.CTkComboBox(
            ctrl, values=["⏳ Conecte-se primeiro..."], width=300, height=32)
        self.combo_monitor.pack(side="left", padx=5)

        self.btn_iniciar_mon = ctk.CTkButton(
            ctrl, text="▶  Iniciar",
            command=self._iniciar_monitoramento,
            fg_color=VERDE, hover_color="#00a67c",
            text_color="black", width=100, state="disabled",
        )
        self.btn_iniciar_mon.pack(side="left", padx=5)

        self.btn_parar_mon = ctk.CTkButton(
            ctrl, text="⏹  Parar",
            command=self._parar_monitoramento,
            fg_color=VERMELHO, hover_color="#cc0000",
            width=100, state="disabled",
        )
        self.btn_parar_mon.pack(side="left", padx=5)

        self.indicador = ctk.CTkLabel(tab, text="⚫ Inativo", text_color="#888")
        self.indicador.pack(pady=5)

        self.feed = ctk.CTkTextbox(tab, font=ctk.CTkFont("Helvetica", 12))
        self.feed.pack(padx=30, pady=5, fill="both", expand=True)

        ctk.CTkButton(tab, text="🗑  Limpar Feed",
                      command=lambda: self.feed.delete("1.0", "end"),
                      height=30, fg_color="#333", hover_color="#444"
                      ).pack(padx=30, pady=5, fill="x")

    # ── Tab Busca ──────────────────────────────────────────────────────
    def _build_tab_busca(self):
        tab = self.tabs.tab("🔍  Busca")
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(2, weight=1)

        ctk.CTkLabel(tab, text="Buscar no Histórico Salvo",
                     font=ctk.CTkFont("Helvetica", 18, "bold")).pack(pady=(20, 5))

        linha = ctk.CTkFrame(tab, fg_color="transparent")
        linha.pack(fill="x", padx=30, pady=10)
        self.entry_busca = ctk.CTkEntry(
            linha,
            placeholder_text="Digite palavra-chave ou nome do grupo...",
            height=38, font=ctk.CTkFont("Helvetica", 13),
        )
        self.entry_busca.pack(side="left", fill="x", expand=True, padx=(0, 10))
        ctk.CTkButton(linha, text="🔍 Buscar",
                      command=self._buscar,
                      fg_color=VERDE, hover_color="#00a67c",
                      text_color="black", width=100, height=38
                      ).pack(side="left")

        self.label_resultado = ctk.CTkLabel(tab, text="", text_color="#aaa")
        self.label_resultado.pack()
        self.txt_busca = ctk.CTkTextbox(tab, font=ctk.CTkFont("Courier", 11))
        self.txt_busca.pack(padx=30, pady=5, fill="both", expand=True)

    # ── Ações ──────────────────────────────────────────────────────────
    def _iniciar_conexao(self):
        if not SELENIUM_OK:
            messagebox.showerror(
                "Erro",
                "Instale as dependências:\npip install selenium webdriver-manager",
            )
            return
        self.btn_conectar.configure(state="disabled", text="⏳ Conectando...")
        self._thread_bot = threading.Thread(target=self._run_bot, daemon=True)
        self._thread_bot.start()

    def _run_bot(self):
        try:
            self.motor = MotorWhatsApp(
                callback_log=self._log,
                callback_msg=self._nova_mensagem_rt,
            )
            if not self.motor.iniciar_chrome():
                return
            if self.motor.aguardar_login():
                self.dot.configure(text="● Conectado", text_color=VERDE)
                self.btn_conectar.configure(text="✅ Conectado", state="disabled")
                time.sleep(2)
                self.lista_grupos = self.motor.listar_grupos()
                if self.lista_grupos:
                    self.combo_hist.configure(values=self.lista_grupos)
                    self.combo_hist.set(self.lista_grupos[0])
                    self.combo_monitor.configure(values=self.lista_grupos)
                    self.combo_monitor.set(self.lista_grupos[0])
                self.btn_extrair.configure(state="normal")
                self.btn_iniciar_mon.configure(state="normal")
                self._log(f"✅ {len(self.lista_grupos)} grupos/chats encontrados.")
            else:
                self.btn_conectar.configure(state="normal", text="🚀  Iniciar Conexão")
        except Exception as e:
            self._log(f"❌ Erro: {e}")
            self.btn_conectar.configure(state="normal", text="🚀  Iniciar Conexão")

    def _desconectar(self):
        if self.motor:
            self.motor.fechar()
            self.motor = None
        self.dot.configure(text="● Desconectado", text_color=VERMELHO)
        self.btn_conectar.configure(state="normal", text="🚀  Iniciar Conexão")
        self._log("🔌 Desconectado.")

    def _extrair_historico(self):
        if not self.motor:
            messagebox.showwarning("Aviso", "Conecte-se primeiro!")
            return
        grupo  = self.combo_hist.get()
        passos = int(self.slider_hist.get())
        self.btn_extrair.configure(state="disabled", text="⏳ Extraindo...")
        self.btn_export_html.configure(state="disabled")
        self.txt_historico.delete("1.0", "end")
        self._msgs_extraidas = []
        self.progress_bar.set(0)

        def task():
            log_orig = self.motor.callback_log

            def log_prog(msg):
                log_orig(msg)
                # Atualiza barra de progresso pelo padrão "[X/Y]"
                if "/" in msg and "[" in msg:
                    try:
                        parte = msg.split("[")[1].split("]")[0].split("/")
                        atual, total = int(parte[0]), int(parte[1])
                        self.progress_bar.set(atual / total)
                    except Exception:
                        pass

            self.motor.callback_log = log_prog
            try:
                msgs = self.motor.extrair_historico_completo(
                    grupo, qtd_passos=passos, passo_px=400
                )
            finally:
                self.motor.callback_log = log_orig

            self._msgs_extraidas = msgs
            self.progress_bar.set(1.0)
            self.txt_historico.insert(
                "end", f"=== {grupo} — {len(msgs)} mensagens ===\n\n"
            )
            for m in msgs:
                icone = "🖼 " if m.get("foto_path") else "💬 "
                self.txt_historico.insert(
                    "end",
                    f"[{m['timestamp']}] {icone}{m['autor']}: {m['texto']}\n",
                )
            self.txt_historico.see("end")
            self.btn_extrair.configure(
                state="normal", text="📥  Extrair Histórico Completo"
            )
            if msgs:
                self.btn_export_html.configure(state="normal")
                self._log(f"✅ {len(msgs)} mensagens! Clique em 🌐 Salvar HTML.")

        threading.Thread(target=task, daemon=True).start()

    def _salvar_html_combinado(self):
        if not self._msgs_extraidas:
            messagebox.showinfo("Aviso", "Extraia o histórico primeiro!")
            return
        path = self.motor.exportar_html_combinado(
            self.combo_hist.get(), self._msgs_extraidas
        )
        if path:
            messagebox.showinfo(
                "Exportado!",
                f"Arquivo salvo em:\n{path}\n\nAbrindo no navegador...",
            )
            if os.name == "nt":
                os.startfile(path)
            else:
                os.system(f"xdg-open '{path}'")
        else:
            messagebox.showerror("Erro", "Falha ao gerar o HTML.")

    def _salvar_historico_txt(self):
        conteudo = self.txt_historico.get("1.0", "end")
        if not conteudo.strip():
            messagebox.showinfo("Aviso", "Nada para salvar.")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".txt",
            filetypes=[("Texto", "*.txt")],
        )
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(conteudo)
            messagebox.showinfo("Sucesso", f"Salvo em:\n{path}")

    def _iniciar_monitoramento(self):
        if not self.motor:
            messagebox.showwarning("Aviso", "Conecte-se primeiro!")
            return
        grupo = self.combo_monitor.get()
        self.btn_iniciar_mon.configure(state="disabled")
        self.btn_parar_mon.configure(state="normal")
        self.indicador.configure(text="🟢 Monitorando...", text_color=VERDE)
        self._thread_mon = threading.Thread(
            target=self.motor.iniciar_monitoramento,
            args=(grupo,), daemon=True,
        )
        self._thread_mon.start()

    def _parar_monitoramento(self):
        if self.motor:
            self.motor.parar_monitoramento()
        self.btn_iniciar_mon.configure(state="normal")
        self.btn_parar_mon.configure(state="disabled")
        self.indicador.configure(text="⚫ Inativo", text_color="#888")

    def _nova_mensagem_rt(self, msg):
        ts    = datetime.now().strftime("%H:%M:%S")
        icone = "🖼 " if msg.get("foto_path") else "💬 "
        self.feed.insert("end", f"[{ts}] {icone}{msg['autor']}: {msg['texto']}\n")
        self.feed.see("end")
        self.indicador.configure(text="🟡 Nova mensagem!", text_color=AMARELO)
        self.after(1500, lambda: self.indicador.configure(
            text="🟢 Monitorando...", text_color=VERDE))

    def _buscar(self):
        if not self.motor:
            messagebox.showwarning("Aviso", "Conecte-se primeiro!")
            return
        termo = self.entry_busca.get().strip()
        rows  = self.motor.buscar_mensagens_db(grupo=termo, limite=200)
        self.txt_busca.delete("1.0", "end")
        self.label_resultado.configure(text=f"{len(rows)} resultados encontrados")
        for grupo, autor, conteudo, ts in rows:
            self.txt_busca.insert(
                "end", f"[{ts[:16]}] [{grupo}] {autor}: {conteudo}\n"
            )

    # ── Utilitários da sidebar ─────────────────────────────────────────
    def _abrir_pasta(self, sub):
        path = os.path.join("dados_whatsapp", sub)
        os.makedirs(path, exist_ok=True)
        if os.name == "nt":
            os.startfile(path)
        else:
            os.system(f"xdg-open '{path}'")

    def _abrir_fotos(self):   self._abrir_pasta("fotos")
    def _abrir_exports(self): self._abrir_pasta("exports")
    def _abrir_logs(self):    self._abrir_pasta("logs")

    def _ver_banco(self):
        if not self.motor:
            messagebox.showinfo("Info", "Conecte-se para consultar o banco.")
            return
        rows = self.motor.buscar_mensagens_db(limite=50)
        self.tabs.set("🔍  Busca")
        self.txt_busca.delete("1.0", "end")
        self.label_resultado.configure(text=f"Últimas {len(rows)} entradas no banco")
        for grupo, autor, conteudo, ts in rows:
            self.txt_busca.insert(
                "end", f"[{ts[:16]}] [{grupo}] {autor}: {conteudo}\n"
            )

    def _toggle_tema(self):
        ctk.set_appearance_mode(self.tema_switch.get())

    def _update_slider_label(self, val):
        v       = int(float(val))
        mn, mx  = v * 10, v * 20
        minutos = round(v * 9 / 60)
        self.label_slider.configure(text=f"{v} passos ≈ ~{mn:,}–{mx:,} msgs")
        self.label_tempo.configure(
            text=f"⏱ Tempo estimado: ~{minutos} min  "
                 "(varia com tamanho do grupo e rede)"
        )

    # ── Log ────────────────────────────────────────────────────────────
    def _log(self, msg, _nivel="info"):
        ts    = datetime.now().strftime("%H:%M:%S")
        linha = f"[{ts}] {msg}\n"
        try:
            self.log_conexao.insert("end", linha)
            self.log_conexao.see("end")
        except Exception:
            pass
        print(linha.strip())

    def _ao_fechar(self):
        if self.motor:
            self.motor.fechar()
        self.destroy()


# ══════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    app = App()
    app.mainloop()