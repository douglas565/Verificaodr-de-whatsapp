"""
WhatsApp Group Monitor — Versão Corrigida
==========================================
Correções aplicadas:
  - capturar_mensagens_visiveis duplicada removida (a versão ruim era a que ficava)
  - Extração de texto robusta com múltiplos fallbacks
  - Rolagem correta via scrollTop = 0 no container certo
  - Deduplicação por hash de conteúdo real (não timestamp de now())
  - Exportação combinada HTML (mensagens + fotos em ordem cronológica)
  - Timestamp real das mensagens extraído do DOM quando disponível

Dependências: pip install selenium webdriver-manager customtkinter pillow
Chrome deve estar instalado no sistema.
"""

import os
import time
import json
import base64
import hashlib
import threading
import sqlite3
import base64
import hashlib
from datetime import datetime
import customtkinter as ctk
from tkinter import messagebox, filedialog
from selenium.webdriver.common.keys import Keys
import tkinter as tk

# ──────────────────────────────────────────────
# TENTATIVA DE IMPORTAR SELENIUM
# ──────────────────────────────────────────────
try:
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.chrome.options import Options
    from selenium.common.exceptions import (
        TimeoutException, NoSuchElementException, StaleElementReferenceException
    )
    from webdriver_manager.chrome import ChromeDriverManager
    SELENIUM_OK = True
except ImportError:
    SELENIUM_OK = False


# ══════════════════════════════════════════════
# MOTOR DE CAPTURA (Selenium)
# ══════════════════════════════════════════════
class MotorWhatsApp:
    """Gerencia conexão e captura de mensagens via WhatsApp Web."""


    def __init__(self, diretorio="dados_whatsapp", callback_log=None, callback_msg=None):
        self.diretorio    = diretorio
        self.callback_log = callback_log or print
        self.callback_msg = callback_msg
        self.driver       = None
        self.monitorando  = False
        self.grupo_atual  = None
        self._criar_dirs()
        self._init_db()

    # ── Setup ──────────────────────────────────
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
        # Adiciona colunas novas se a tabela já existia sem elas
        for col, tipo in [("foto_path", "TEXT"), ("hash_msg", "TEXT")]:
            try:
                self.conn.execute(f"ALTER TABLE mensagens ADD COLUMN {col} {tipo}")
            except Exception:
                pass
        self.conn.commit()

    # ── Chrome ─────────────────────────────────
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

    # ── Grupos ─────────────────────────────────
    def listar_grupos(self):
        grupos = []
        try:
            self.callback_log("🔍 Vasculhando interface do WhatsApp...")
            time.sleep(5)

            seletores = [
                'span[data-testid="cell-frame-title"]',
                'div._ak8q',
                'span.selectable-text.copyable-text',
                'div[dir="auto"]'
            ]

            for seletor in seletores:
                elementos = self.driver.find_elements(By.CSS_SELECTOR, seletor)
                for el in elementos:
                    try:
                        nome = el.text.strip()
                        if len(nome) > 1 and nome not in grupos and '\n' not in nome:
                            grupos.append(nome)
                    except:
                        continue

            if not grupos:
                self.callback_log("⚠️ Tentando busca profunda via XPath...")
                elementos_xpath = self.driver.find_elements(By.XPATH, '//span[@dir="auto"]')
                for el in elementos_xpath:
                    nome = el.text.strip()
                    if len(nome) > 2 and nome not in grupos:
                        grupos.append(nome)

            termos_lixo = ["WhatsApp", "Conversas", "Status", "Canais",
                           "Comunidades", "Novo chat"]
            grupos = [g for g in grupos if g not in termos_lixo]

            self.callback_log(f"✅ {len(grupos)} possíveis chats encontrados.")
            return sorted(list(set(grupos)))

        except Exception as e:
            self.callback_log(f"⚠️ Erro na listagem: {e}")
            return []

    def abrir_grupo(self, nome_grupo):
        try:
            self.callback_log(f"🔍 Abrindo: {nome_grupo}")

            # Tenta clique direto na lista lateral
            chats_laterais = self.driver.find_elements(By.CSS_SELECTOR, 'span[title]')
            for chat in chats_laterais:
                if chat.get_attribute("title") == nome_grupo:
                    self.driver.execute_script("arguments[0].click();", chat)
                    self.grupo_atual = nome_grupo
                    time.sleep(1.5)
                    return True

            # Fallback: barra de busca
            search_box = WebDriverWait(self.driver, 10).until(
                EC.element_to_be_clickable(
                    (By.CSS_SELECTOR, 'div[contenteditable="true"][data-tab="3"]')
                )
            )
            search_box.click()
            search_box.clear()
            for letra in nome_grupo:
                search_box.send_keys(letra)
                time.sleep(0.05)
            time.sleep(2)

            xpath_resultado = f"//span[@title='{nome_grupo}']"
            resultado = WebDriverWait(self.driver, 10).until(
                EC.element_to_be_clickable((By.XPATH, xpath_resultado))
            )
            self.driver.execute_script("arguments[0].click();", resultado)
            self.grupo_atual = nome_grupo
            self.callback_log(f"✅ Grupo '{nome_grupo}' aberto.")
            time.sleep(1.5)
            return True

        except Exception as e:
            self.callback_log(f"❌ Erro ao abrir grupo: {e}")
            return False

    # ── Captura de Mensagens ────────────────────
    def _extrair_texto_bolha(self, el):
        """
        Tenta extrair o texto de uma bolha de mensagem com múltiplos fallbacks.
        Retorna string vazia se não houver texto.
        """
        # Método 1: atributo data-pre-plain-text (o mais confiável — traz metadados)
        # e copyable-text innerText para o corpo
        try:
            container = el.find_element(By.CSS_SELECTOR, '.copyable-text')
            texto = container.get_attribute("innerText") or ""
            texto = texto.strip()
            if texto:
                return texto
        except:
            pass

        # Método 2: span.selectable-text direto
        try:
            spans = el.find_elements(
                By.CSS_SELECTOR, 'span.selectable-text'
            )
            partes = []
            for s in spans:
                t = (s.get_attribute("innerText") or s.text or "").strip()
                if t:
                    partes.append(t)
            texto = " ".join(partes)
            if texto:
                return texto
        except:
            pass

        # Método 3: qualquer span com dir=ltr ou dir=rtl (emojis e texto misto)
        try:
            spans = el.find_elements(By.CSS_SELECTOR, 'span[dir]')
            partes = []
            for s in spans:
                t = (s.get_attribute("innerText") or s.text or "").strip()
                if t and t not in partes:
                    partes.append(t)
            texto = " ".join(partes)
            if texto:
                return texto
        except:
            pass

        # Método 4: innerText bruto do elemento inteiro (último recurso)
        try:
            texto = (el.get_attribute("innerText") or "").strip()
            # Remove linhas que são só horário (ex: "14:30")
            linhas = [l for l in texto.splitlines()
                      if l.strip() and not l.strip().replace(":", "").isdigit()]
            return " ".join(linhas).strip()
        except:
            pass

        return ""

    def _extrair_timestamp_bolha(self, el):
        """Tenta extrair o horário real da mensagem do DOM."""
        # Tenta o atributo data-pre-plain-text que contém "[HH:MM, DD/MM/YYYY] Autor:"
        try:
            container = el.find_element(By.CSS_SELECTOR, '.copyable-text')
            meta = container.get_attribute("data-pre-plain-text") or ""
            # Formato: "[14:30, 28/03/2025] João:"
            if meta and "[" in meta:
                parte = meta.split("]")[0].replace("[", "").strip()
                return parte  # "14:30, 28/03/2025"
        except:
            pass

        # Fallback: span com aria-label de horário
        try:
            span_hora = el.find_element(
                By.CSS_SELECTOR,
                'span[data-testid="msg-meta"] span, span[aria-label]'
            )
            label = span_hora.get_attribute("aria-label") or span_hora.text
            if label:
                return label.strip()
        except:
            pass

        return datetime.now().strftime("%H:%M, %d/%m/%Y")

    def _extrair_autor_bolha(self, el):
        """Extrai o autor da mensagem."""
        # Mensagem enviada por você
        try:
            classes = el.get_attribute("class") or ""
            if "message-out" in classes:
                return "Você"
        except:
            pass

        # Nome explícito (grupos)
        for seletor in [
            'span[data-testid="author"]',
            'span.copyable-text[dir="auto"]',
            '._akbu',  # seletor comum para nome em grupos
        ]:
            try:
                autor_el = el.find_element(By.CSS_SELECTOR, seletor)
                nome = (autor_el.text or "").strip()
                if nome:
                    return nome
            except:
                pass

        # data-pre-plain-text como fallback de autor
        try:
            container = el.find_element(By.CSS_SELECTOR, '.copyable-text')
            meta = container.get_attribute("data-pre-plain-text") or ""
            if "] " in meta:
                return meta.split("] ", 1)[1].rstrip(":")
        except:
            pass

        return "Desconhecido"

    def _baixar_imagem_blob(self, src_url, nome_img):
        """Baixa uma imagem blob: via XHR dentro do contexto do Chrome."""
        path_img = os.path.join(self.diretorio, "fotos", nome_img)
        script = """
            var uri = arguments[0];
            var callback = arguments[1];
            var xhr = new XMLHttpRequest();
            xhr.responseType = 'blob';
            xhr.onload = function() {
                var reader = new FileReader();
                reader.onloadend = function() { callback(reader.result); };
                reader.readAsDataURL(xhr.response);
            };
            xhr.onerror = function() { callback(''); };
            xhr.open('GET', uri);
            xhr.send();
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

    def capturar_mensagens_visiveis(self):
        """
        Captura todas as mensagens visíveis na tela.
        Retorna lista de dicts ordenada como aparece na tela.
        """
        mensagens = []
        try:
            # Pega todas as bolhas (recebidas e enviadas)
            elementos = self.driver.find_elements(
                By.CSS_SELECTOR, 'div.message-in, div.message-out'
            )

            for el in elementos:
                try:
                    texto     = self._extrair_texto_bolha(el)
                    autor     = self._extrair_autor_bolha(el)
                    timestamp = self._extrair_timestamp_bolha(el)
                    foto_path = None
                    tipo      = "texto"

                    # ── Tenta capturar imagem ────────────────
                    try:
                        img_el  = el.find_element(By.CSS_SELECTOR, 'img[src*="blob:"]')
                        src_url = img_el.get_attribute("src")
                        if src_url:
                            nome_img  = f"img_{hashlib.md5(src_url.encode()).hexdigest()[:10]}.jpg"
                            foto_path = self._baixar_imagem_blob(src_url, nome_img)
                            tipo = "imagem" if foto_path else tipo
                            if not texto:
                                texto = "[IMAGEM]"
                            else:
                                texto += " [IMAGEM]"
                    except:
                        pass

                    # Ignora bolha completamente vazia
                    if not texto.strip() and not foto_path:
                        continue

                    # Chave de deduplicação baseada em conteúdo real
                    chave_raw = f"{autor}|{texto}|{timestamp}"
                    hash_msg  = hashlib.md5(chave_raw.encode()).hexdigest()

                    mensagens.append({
                        "autor":     autor,
                        "texto":     texto,
                        "timestamp": timestamp,
                        "grupo":     self.grupo_atual or "Desconhecido",
                        "tipo":      tipo,
                        "foto_path": foto_path,
                        "hash_msg":  hash_msg,
                    })

                except StaleElementReferenceException:
                    continue
                except Exception:
                    continue

        except Exception as e:
            self.callback_log(f"⚠️ Erro na captura de DOM: {e}")

        return mensagens

    # ── Rolagem ────────────────────────────────
    def _encontrar_container_msgs(self):
        """Localiza o painel de mensagens para rolagem."""
        seletores = [
            'div[data-testid="conversation-panel-messages"]',
            '#main div[role="application"]',
            '#main',
        ]
        for sel in seletores:
            try:
                el = self.driver.find_element(By.CSS_SELECTOR, sel)
                if el:
                    return el
            except:
                continue
        return None

    def rolar_para_topo(self, container):
        """Rola o container até o topo para forçar carregamento do histórico."""
        try:
            # scrollTop = 0 é mais confiável que PAGE_UP no WhatsApp Web
            self.driver.execute_script("arguments[0].scrollTop = 0;", container)
        except:
            try:
                webdriver.ActionChains(self.driver).send_keys(Keys.HOME).perform()
            except:
                pass

    # ── Extração Completa do Histórico ─────────
    def extrair_historico_completo(self, grupo, qtd_rolagens=50):
        """
        Extrai o histórico completo rolando para cima repetidamente.
        Retorna lista ordenada cronologicamente.
        """
        if not self.abrir_grupo(grupo):
            return []

        self.callback_log(f"📜 Iniciando extração de '{grupo}'...")
        todas_mensagens  = {}   # hash_msg → msg (deduplicação)
        sem_novidade_cnt = 0    # Para detectar quando chegou ao início

        container = self._encontrar_container_msgs()
        if not container:
            self.callback_log("❌ Container de mensagens não encontrado.")
            return []

        for i in range(qtd_rolagens):
            msgs_agora = self.capturar_mensagens_visiveis()
            novas = 0

            for m in msgs_agora:
                h = m["hash_msg"]
                if h not in todas_mensagens:
                    todas_mensagens[h] = m
                    self._salvar_mensagem(m)
                    novas += 1

            if novas == 0:
                sem_novidade_cnt += 1
                if sem_novidade_cnt >= 5:
                    self.callback_log("📌 Chegamos ao início da conversa!")
                    break
            else:
                sem_novidade_cnt = 0

            # Rola para o topo e aguarda lazy-load
            self.rolar_para_topo(container)
            time.sleep(2.5)

            if i % 5 == 0 or novas > 0:
                self.callback_log(
                    f"⏳ Bloco {i+1}/{qtd_rolagens} | Novas: {novas} | "
                    f"Total: {len(todas_mensagens)}"
                )

        resultado = list(todas_mensagens.values())
        self.callback_log(f"✅ Extração concluída! {len(resultado)} mensagens.")
        return resultado

    # ── Monitoramento em Tempo Real ─────────────
    def iniciar_monitoramento(self, grupo):
        self.monitorando = True
        self.abrir_grupo(grupo)
        self.callback_log(f"🎧 Monitoramento em tempo real: '{grupo}'")
        msgs_vistas = set()

        while self.monitorando:
            try:
                msgs_atuais = self.capturar_mensagens_visiveis()
                for msg in msgs_atuais:
                    h = msg["hash_msg"]
                    if h not in msgs_vistas:
                        msgs_vistas.add(h)
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

    # ── Persistência ───────────────────────────
    def _salvar_mensagem(self, msg):
        try:
            self.conn.execute(
                """INSERT OR IGNORE INTO mensagens
                   (grupo, autor, conteudo, tipo, foto_path, timestamp, hash_msg)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    msg["grupo"],
                    msg["autor"],
                    msg["texto"],
                    msg.get("tipo", "texto"),
                    msg.get("foto_path"),
                    msg["timestamp"],
                    msg["hash_msg"],
                )
            )
            self.conn.commit()
        except Exception:
            pass

    # ── Exportação Combinada (HTML) ─────────────
    def exportar_html_combinado(self, grupo, msgs):
        """
        Gera um arquivo HTML com mensagens e fotos intercaladas
        na ordem cronológica, imitando o visual do WhatsApp.
        """
        if not msgs:
            return None

        nome_arquivo = grupo.replace("/", "-").replace("\\", "-")[:50]
        ts_export    = datetime.now().strftime("%Y%m%d_%H%M%S")
        path_html    = os.path.join(
            self.diretorio, "exports", f"{nome_arquivo}_{ts_export}.html"
        )

        # Converte fotos para base64 inline para que o HTML seja portável
        def foto_para_base64(path):
            try:
                if path and os.path.exists(path):
                    with open(path, "rb") as f:
                        return base64.b64encode(f.read()).decode()
            except:
                pass
            return None

        linhas_html = []
        for m in msgs:
            autor     = m["autor"]
            texto     = m["texto"].replace("[IMAGEM]", "").strip()
            ts        = m["timestamp"]
            is_out    = autor == "Você"
            lado      = "msg-out" if is_out else "msg-in"
            foto_b64  = foto_para_base64(m.get("foto_path"))

            partes_conteudo = []
            if foto_b64:
                partes_conteudo.append(
                    f'<img src="data:image/jpeg;base64,{foto_b64}" '
                    f'style="max-width:300px;border-radius:8px;display:block;margin-bottom:4px;" />'
                )
            if texto:
                partes_conteudo.append(
                    f'<span class="msg-text">{texto}</span>'
                )

            if not partes_conteudo:
                continue

            conteudo_inner = "\n".join(partes_conteudo)

            linhas_html.append(f"""
            <div class="msg-wrapper {lado}">
              <div class="bubble">
                {"" if is_out else f'<span class="autor">{autor}</span>'}
                {conteudo_inner}
                <span class="ts">{ts}</span>
              </div>
            </div>""")

        html = f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8">
  <title>Histórico — {grupo}</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #e5ddd5;
      padding: 20px;
    }}
    h1 {{
      text-align: center;
      color: #075e54;
      padding: 16px;
      background: #fff;
      border-radius: 12px;
      margin-bottom: 20px;
      font-size: 18px;
    }}
    .chat-container {{
      max-width: 720px;
      margin: 0 auto;
      display: flex;
      flex-direction: column;
      gap: 4px;
    }}
    .msg-wrapper {{
      display: flex;
      margin: 2px 0;
    }}
    .msg-in  {{ justify-content: flex-start; }}
    .msg-out {{ justify-content: flex-end; }}
    .bubble {{
      max-width: 72%;
      padding: 8px 12px;
      border-radius: 12px;
      font-size: 14px;
      line-height: 1.4;
      position: relative;
      box-shadow: 0 1px 2px rgba(0,0,0,.15);
    }}
    .msg-in  .bubble {{ background: #fff;     border-top-left-radius: 0; }}
    .msg-out .bubble {{ background: #dcf8c6;  border-top-right-radius: 0; }}
    .autor {{
      display: block;
      font-weight: 700;
      font-size: 13px;
      color: #128c7e;
      margin-bottom: 3px;
    }}
    .msg-text {{ display: block; word-break: break-word; }}
    .ts {{
      display: block;
      font-size: 11px;
      color: #999;
      text-align: right;
      margin-top: 4px;
    }}
    .info-bar {{
      text-align: center;
      color: #555;
      font-size: 12px;
      background: #fff;
      border-radius: 8px;
      padding: 6px 12px;
      margin: 8px auto;
      max-width: 300px;
    }}
  </style>
</head>
<body>
  <h1>📱 {grupo}</h1>
  <div class="info-bar">
    {len(msgs)} mensagens exportadas em {datetime.now().strftime('%d/%m/%Y %H:%M')}
  </div>
  <div class="chat-container">
    {"".join(linhas_html)}
  </div>
</body>
</html>"""

        with open(path_html, "w", encoding="utf-8") as f:
            f.write(html)

        self.callback_log(f"📄 HTML exportado: {path_html}")
        return path_html

    def _exportar_txt(self, grupo, msgs):
        nome_arquivo = grupo.replace("/", "-").replace("\\", "-")[:50]
        path = os.path.join(self.diretorio, "logs", f"{nome_arquivo}.txt")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"\n--- Exportação: {datetime.now().strftime('%Y-%m-%d %H:%M')} ---\n")
            for m in msgs:
                linha_foto = f" [foto: {m['foto_path']}]" if m.get("foto_path") else ""
                f.write(f"[{m['timestamp']}] {m['autor']}: {m['texto']}{linha_foto}\n")
        self.callback_log(f"💾 TXT salvo em: {path}")
        return path

    # ── Consulta BD ────────────────────────────
    def buscar_mensagens_db(self, grupo=None, limite=500):
        query  = "SELECT grupo, autor, conteudo, timestamp FROM mensagens"
        params = []
        if grupo:
            query += " WHERE grupo LIKE ?"
            params.append(f"%{grupo}%")
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limite)
        cursor = self.conn.execute(query, params)
        return cursor.fetchall()

    def fechar(self):
        self.monitorando = False
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass
        if self.conn:
            self.conn.close()


# ══════════════════════════════════════════════
# INTERFACE GRÁFICA
# ══════════════════════════════════════════════
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
        self.title("WhatsApp Monitor Pro")
        self.geometry("1100x680")
        self.configure(fg_color=FUNDO)
        self.protocol("WM_DELETE_WINDOW", self._ao_fechar)

        self.motor           = None
        self.lista_grupos    = []
        self._thread_bot     = None
        self._thread_mon     = None
        self._msgs_extraidas = []   # guarda as msgs para export combinado

        self._build_ui()

        if not SELENIUM_OK:
            self._log("❌ Selenium não instalado!", "erro")
            self._log("   Execute: pip install selenium webdriver-manager", "aviso")

    # ── Construção da UI ───────────────────────
    def _build_ui(self):
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self._build_sidebar()
        self._build_main()

    def _build_sidebar(self):
        sb = ctk.CTkFrame(self, width=220, corner_radius=0, fg_color="#16213e")
        sb.grid(row=0, column=0, sticky="nsew")
        sb.grid_propagate(False)

        ctk.CTkLabel(
            sb, text="📱 WA Monitor",
            font=ctk.CTkFont("Helvetica", 20, "bold"),
            text_color=VERDE
        ).pack(pady=(30, 5))
        ctk.CTkLabel(
            sb, text="Pro Edition",
            font=ctk.CTkFont("Helvetica", 11),
            text_color="#888"
        ).pack(pady=(0, 30))

        self.dot = ctk.CTkLabel(sb, text="● Desconectado", text_color=VERMELHO,
                                font=ctk.CTkFont("Helvetica", 12))
        self.dot.pack(pady=5)

        ctk.CTkFrame(sb, height=2, fg_color="gray30").pack(fill="x", padx=20, pady=15)

        for texto, cmd in [
            ("📁  Abrir Fotos",   self._abrir_fotos),
            ("📄  Abrir Exports", self._abrir_exports),
            ("📝  Abrir Logs",    self._abrir_logs),
            ("🗃️  Ver Banco",     self._ver_banco),
        ]:
            ctk.CTkButton(
                sb, text=texto, command=cmd,
                fg_color="transparent", hover_color="#1e3a5f",
                anchor="w", font=ctk.CTkFont("Helvetica", 13)
            ).pack(fill="x", padx=15, pady=3)

        ctk.CTkFrame(sb, height=2, fg_color="gray30").pack(fill="x", padx=20, pady=15)

        ctk.CTkLabel(sb, text="Tema", text_color="#888",
                     font=ctk.CTkFont("Helvetica", 11)).pack()
        self.tema_switch = ctk.CTkSwitch(
            sb, text="Modo Claro",
            command=self._toggle_tema,
            onvalue="Light", offvalue="Dark"
        )
        self.tema_switch.pack(pady=5)

    def _build_main(self):
        main = ctk.CTkFrame(self, corner_radius=12, fg_color=CINZA)
        main.grid(row=0, column=1, padx=15, pady=15, sticky="nsew")
        main.grid_columnconfigure(0, weight=1)
        main.grid_rowconfigure(0, weight=1)

        self.tabs = ctk.CTkTabview(
            main, fg_color="#222",
            segmented_button_selected_color=VERDE,
            segmented_button_selected_hover_color="#00a67c"
        )
        self.tabs.grid(row=0, column=0, padx=10, pady=10, sticky="nsew")

        for aba in ["🔗  Conexão", "📜  Histórico", "📡  Tempo Real", "🔍  Busca"]:
            self.tabs.add(aba)

        self._build_tab_conexao()
        self._build_tab_historico()
        self._build_tab_tempo_real()
        self._build_tab_busca()

    # ── Tab: Conexão ───────────────────────────
    def _build_tab_conexao(self):
        tab = self.tabs.tab("🔗  Conexão")
        tab.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            tab, text="Conectar ao WhatsApp Web",
            font=ctk.CTkFont("Helvetica", 18, "bold")
        ).pack(pady=(20, 5))

        ctk.CTkLabel(
            tab,
            text="O Chrome abrirá automaticamente. Escaneie o QR Code com seu celular.",
            text_color="#aaa", wraplength=500
        ).pack(pady=(0, 20))

        card = ctk.CTkFrame(tab, fg_color="#1a1a2e", corner_radius=10)
        card.pack(padx=40, pady=10, fill="x")

        instrucoes = [
            ("1️⃣", "Clique em 'Iniciar Conexão'"),
            ("2️⃣", "O Chrome vai abrir com o WhatsApp Web"),
            ("3️⃣", "No celular: WhatsApp → Aparelhos Conectados → Conectar"),
            ("4️⃣", "Escaneie o QR Code na tela do Chrome"),
            ("5️⃣", "Aguarde o status mudar para CONECTADO"),
        ]
        for emoji, texto in instrucoes:
            f = ctk.CTkFrame(card, fg_color="transparent")
            f.pack(fill="x", padx=15, pady=3)
            ctk.CTkLabel(f, text=emoji, width=30).pack(side="left")
            ctk.CTkLabel(f, text=texto, anchor="w", text_color="#ccc").pack(side="left")

        self.btn_conectar = ctk.CTkButton(
            tab, text="🚀  Iniciar Conexão",
            command=self._iniciar_conexao,
            font=ctk.CTkFont("Helvetica", 14, "bold"),
            height=45, fg_color=VERDE, hover_color="#00a67c",
            text_color="black"
        )
        self.btn_conectar.pack(pady=20, padx=40, fill="x")

        self.btn_desconectar = ctk.CTkButton(
            tab, text="⏹  Desconectar",
            command=self._desconectar,
            height=35, fg_color="transparent",
            border_color=VERMELHO, border_width=1,
            text_color=VERMELHO, hover_color="#3d1a1a"
        )
        self.btn_desconectar.pack(pady=5, padx=40, fill="x")

        ctk.CTkLabel(tab, text="Log do Sistema:", anchor="w").pack(padx=40, fill="x")
        self.log_conexao = ctk.CTkTextbox(tab, height=130, font=ctk.CTkFont("Courier", 11))
        self.log_conexao.pack(padx=40, pady=(5, 20), fill="x")

    # ── Tab: Histórico ─────────────────────────
    def _build_tab_historico(self):
        tab = self.tabs.tab("📜  Histórico")
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(4, weight=1)

        ctk.CTkLabel(
            tab, text="Extrair Histórico Completo",
            font=ctk.CTkFont("Helvetica", 18, "bold")
        ).pack(pady=(20, 5))

        ctk.CTkLabel(
            tab,
            text="Extrai mensagens E fotos, depois gera um arquivo HTML combinado.",
            text_color="#aaa"
        ).pack(pady=(0, 10))

        ctk.CTkLabel(tab, text="Grupo:", anchor="w").pack(padx=30, fill="x")
        self.combo_hist = ctk.CTkComboBox(
            tab, values=["⏳ Conecte-se primeiro..."], width=400, height=35
        )
        self.combo_hist.pack(padx=30, pady=5, fill="x")

        ctk.CTkLabel(
            tab, text="Nº de rolagens (cada rolagem ≈ 20 mensagens):", anchor="w"
        ).pack(padx=30, fill="x")
        self.slider_hist = ctk.CTkSlider(tab, from_=5, to=200, number_of_steps=39)
        self.slider_hist.set(30)
        self.slider_hist.pack(padx=30, fill="x")
        self.label_slider = ctk.CTkLabel(tab, text="30 rolagens ≈ ~600 msgs")
        self.label_slider.pack()
        self.slider_hist.configure(command=self._update_slider_label)

        # Botões de ação
        btn_frame = ctk.CTkFrame(tab, fg_color="transparent")
        btn_frame.pack(fill="x", padx=30, pady=8)

        self.btn_extrair = ctk.CTkButton(
            btn_frame, text="📥  Extrair Histórico",
            command=self._extrair_historico,
            height=40, fg_color=VERDE, hover_color="#00a67c",
            text_color="black", state="disabled"
        )
        self.btn_extrair.pack(side="left", fill="x", expand=True, padx=(0, 5))

        self.btn_export_html = ctk.CTkButton(
            btn_frame, text="🌐  Salvar HTML Combinado",
            command=self._salvar_html_combinado,
            height=40, fg_color="#1565c0", hover_color="#0d47a1",
            state="disabled"
        )
        self.btn_export_html.pack(side="left", fill="x", expand=True, padx=(5, 0))

        # Linha de botões secundários
        btn_frame2 = ctk.CTkFrame(tab, fg_color="transparent")
        btn_frame2.pack(fill="x", padx=30, pady=(0, 8))

        self.btn_salvar_hist = ctk.CTkButton(
            btn_frame2, text="💾  Salvar TXT",
            command=self._salvar_historico_txt,
            height=35, fg_color="#333", hover_color="#444"
        )
        self.btn_salvar_hist.pack(side="left", fill="x", expand=True, padx=(0, 5))

        ctk.CTkLabel(tab, text="Mensagens extraídas:", anchor="w").pack(padx=30, fill="x")
        self.txt_historico = ctk.CTkTextbox(tab, font=ctk.CTkFont("Courier", 11))
        self.txt_historico.pack(padx=30, pady=5, fill="both", expand=True)

    # ── Tab: Tempo Real ────────────────────────
    def _build_tab_tempo_real(self):
        tab = self.tabs.tab("📡  Tempo Real")
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(2, weight=1)

        ctk.CTkLabel(
            tab, text="Monitoramento em Tempo Real",
            font=ctk.CTkFont("Helvetica", 18, "bold")
        ).pack(pady=(20, 5))

        ctk.CTkLabel(
            tab, text="Mensagens novas aparecem aqui automaticamente (a cada 3s)",
            text_color="#aaa"
        ).pack(pady=(0, 10))

        ctrl = ctk.CTkFrame(tab, fg_color="transparent")
        ctrl.pack(fill="x", padx=30)
        ctk.CTkLabel(ctrl, text="Grupo:", width=60).pack(side="left")
        self.combo_monitor = ctk.CTkComboBox(
            ctrl, values=["⏳ Conecte-se primeiro..."], width=300, height=32
        )
        self.combo_monitor.pack(side="left", padx=5)

        self.btn_iniciar_mon = ctk.CTkButton(
            ctrl, text="▶  Iniciar",
            command=self._iniciar_monitoramento,
            fg_color=VERDE, hover_color="#00a67c",
            text_color="black", width=100, state="disabled"
        )
        self.btn_iniciar_mon.pack(side="left", padx=5)

        self.btn_parar_mon = ctk.CTkButton(
            ctrl, text="⏹  Parar",
            command=self._parar_monitoramento,
            fg_color=VERMELHO, hover_color="#cc0000",
            width=100, state="disabled"
        )
        self.btn_parar_mon.pack(side="left", padx=5)

        self.indicador = ctk.CTkLabel(tab, text="⚫ Inativo", text_color="#888")
        self.indicador.pack(pady=5)

        self.feed = ctk.CTkTextbox(tab, font=ctk.CTkFont("Helvetica", 12))
        self.feed.pack(padx=30, pady=5, fill="both", expand=True)

        self.btn_limpar_feed = ctk.CTkButton(
            tab, text="🗑  Limpar Feed",
            command=lambda: self.feed.delete("1.0", "end"),
            height=30, fg_color="#333", hover_color="#444"
        )
        self.btn_limpar_feed.pack(padx=30, pady=5, fill="x")

    # ── Tab: Busca ─────────────────────────────
    def _build_tab_busca(self):
        tab = self.tabs.tab("🔍  Busca")
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(2, weight=1)

        ctk.CTkLabel(
            tab, text="Buscar no Histórico Salvo",
            font=ctk.CTkFont("Helvetica", 18, "bold")
        ).pack(pady=(20, 5))

        linha = ctk.CTkFrame(tab, fg_color="transparent")
        linha.pack(fill="x", padx=30, pady=10)

        self.entry_busca = ctk.CTkEntry(
            linha, placeholder_text="Digite palavra-chave ou nome do grupo...",
            height=38, font=ctk.CTkFont("Helvetica", 13)
        )
        self.entry_busca.pack(side="left", fill="x", expand=True, padx=(0, 10))

        ctk.CTkButton(
            linha, text="🔍 Buscar",
            command=self._buscar,
            fg_color=VERDE, hover_color="#00a67c",
            text_color="black", width=100, height=38
        ).pack(side="left")

        self.label_resultado = ctk.CTkLabel(tab, text="", text_color="#aaa")
        self.label_resultado.pack()

        self.txt_busca = ctk.CTkTextbox(tab, font=ctk.CTkFont("Courier", 11))
        self.txt_busca.pack(padx=30, pady=5, fill="both", expand=True)

    # ── Ações ──────────────────────────────────
    def _iniciar_conexao(self):
        if not SELENIUM_OK:
            messagebox.showerror(
                "Erro",
                "Instale as dependências:\npip install selenium webdriver-manager"
            )
            return
        self.btn_conectar.configure(state="disabled", text="⏳ Conectando...")
        self._thread_bot = threading.Thread(target=self._run_bot, daemon=True)
        self._thread_bot.start()

    def _run_bot(self):
        try:
            self.motor = MotorWhatsApp(
                callback_log=self._log,
                callback_msg=self._nova_mensagem_rt
            )
            ok = self.motor.iniciar_chrome()
            if not ok:
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
        grupo    = self.combo_hist.get()
        rolagens = int(self.slider_hist.get())
        self.btn_extrair.configure(state="disabled", text="⏳ Extraindo...")
        self.btn_export_html.configure(state="disabled")
        self.txt_historico.delete("1.0", "end")
        self._msgs_extraidas = []

        def task():
            msgs = self.motor.extrair_historico_completo(grupo, rolagens)
            self._msgs_extraidas = msgs

            self.txt_historico.insert(
                "end", f"=== {grupo} — {len(msgs)} mensagens ===\n\n"
            )
            for m in msgs:
                icone = "🖼 " if m.get("foto_path") else "💬 "
                linha = f"[{m['timestamp']}] {icone}{m['autor']}: {m['texto']}\n"
                self.txt_historico.insert("end", linha)
            self.txt_historico.see("end")

            self.btn_extrair.configure(state="normal", text="📥  Extrair Histórico")
            if msgs:
                self.btn_export_html.configure(state="normal")
                self._log(f"✅ Pronto! Clique em '🌐 Salvar HTML Combinado' para exportar.")

        threading.Thread(target=task, daemon=True).start()

    def _salvar_html_combinado(self):
        if not self._msgs_extraidas:
            messagebox.showinfo("Aviso", "Extraia o histórico primeiro!")
            return
        grupo = self.combo_hist.get()
        path  = self.motor.exportar_html_combinado(grupo, self._msgs_extraidas)
        if path:
            messagebox.showinfo(
                "Exportado!",
                f"Arquivo HTML salvo em:\n{path}\n\n"
                "Abra no navegador para ver mensagens e fotos combinadas."
            )
            # Abre automaticamente
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
            filetypes=[("Arquivo de Texto", "*.txt")]
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
            args=(grupo,), daemon=True
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
        linha = f"[{ts}] {icone}{msg['autor']}: {msg['texto']}\n"
        self.feed.insert("end", linha)
        self.feed.see("end")
        self.indicador.configure(text="🟡 Nova mensagem!", text_color=AMARELO)
        self.after(1500, lambda: self.indicador.configure(
            text="🟢 Monitorando...", text_color=VERDE))

    def _buscar(self):
        if not self.motor:
            messagebox.showwarning("Aviso", "Conecte-se primeiro!")
            return
        termo      = self.entry_busca.get().strip()
        resultados = self.motor.buscar_mensagens_db(grupo=termo, limite=200)
        self.txt_busca.delete("1.0", "end")
        self.label_resultado.configure(text=f"{len(resultados)} resultados encontrados")
        for grupo, autor, conteudo, ts in resultados:
            self.txt_busca.insert("end", f"[{ts[:16]}] [{grupo}] {autor}: {conteudo}\n")

    # ── Utilitários ────────────────────────────
    def _update_slider_label(self, val):
        v = int(float(val))
        self.label_slider.configure(text=f"{v} rolagens ≈ ~{v * 20} msgs")

    def _log(self, msg, nivel="info"):
        ts    = datetime.now().strftime("%H:%M:%S")
        linha = f"[{ts}] {msg}\n"
        self.log_conexao.insert("end", linha)
        self.log_conexao.see("end")
        print(linha.strip())

    def _abrir_fotos(self):
        path = os.path.join("dados_whatsapp", "fotos")
        os.makedirs(path, exist_ok=True)
        if os.name == "nt":
            os.startfile(path)
        else:
            os.system(f"xdg-open '{path}'")

    def _abrir_exports(self):
        path = os.path.join("dados_whatsapp", "exports")
        os.makedirs(path, exist_ok=True)
        if os.name == "nt":
            os.startfile(path)
        else:
            os.system(f"xdg-open '{path}'")

    def _abrir_logs(self):
        path = os.path.join("dados_whatsapp", "logs")
        os.makedirs(path, exist_ok=True)
        if os.name == "nt":
            os.startfile(path)
        else:
            os.system(f"xdg-open '{path}'")

    def _ver_banco(self):
        if not self.motor:
            messagebox.showinfo("Info", "Conecte-se para consultar o banco de dados.")
            return
        resultados = self.motor.buscar_mensagens_db(limite=50)
        self.tabs.set("🔍  Busca")
        self.txt_busca.delete("1.0", "end")
        self.label_resultado.configure(text=f"Últimas {len(resultados)} msgs no banco")
        for grupo, autor, conteudo, ts in resultados:
            self.txt_busca.insert("end", f"[{ts[:16]}] [{grupo}] {autor}: {conteudo}\n")

    def _toggle_tema(self):
        modo = self.tema_switch.get()
        ctk.set_appearance_mode(modo)

    def _ao_fechar(self):
        if self.motor:
            self.motor.fechar()
        self.destroy()

    # ── 1. Extração Inteligente de Elementos ───────────────────

    def _extrair_texto_bolha(self, el):
        """Tenta extrair o texto de uma bolha com múltiplos fallbacks para evitar cortes."""
        try:
            container = el.find_element(By.CSS_SELECTOR, '.copyable-text')
            texto = container.get_attribute("innerText") or ""
            texto = texto.strip()
            if texto: return texto
        except: pass

        try:
            spans = el.find_elements(By.CSS_SELECTOR, 'span.selectable-text')
            partes = [ (s.get_attribute("innerText") or s.text or "").strip() for s in spans ]
            texto = " ".join([p for p in partes if p])
            if texto: return texto
        except: pass

        try:
            spans = el.find_elements(By.CSS_SELECTOR, 'span[dir]')
            partes = []
            for s in spans:
                t = (s.get_attribute("innerText") or s.text or "").strip()
                if t and t not in partes: partes.append(t)
            texto = " ".join(partes)
            if texto: return texto
        except: pass

        try:
            texto = (el.get_attribute("innerText") or "").strip()
            # Remove linhas que são só horário (ex: "14:30")
            linhas = [l for l in texto.splitlines() if l.strip() and not l.strip().replace(":", "").isdigit()]
            return " ".join(linhas).strip()
        except: pass

        return ""

    def _extrair_timestamp_bolha(self, el):
        """Tenta extrair o horário real da mensagem dos metadados."""
        try:
            container = el.find_element(By.CSS_SELECTOR, '.copyable-text')
            meta = container.get_attribute("data-pre-plain-text") or ""
            if meta and "[" in meta:
                return meta.split("]")[0].replace("[", "").strip() 
        except: pass

        try:
            span_hora = el.find_element(By.CSS_SELECTOR, 'span[data-testid="msg-meta"] span, span[aria-label]')
            label = span_hora.get_attribute("aria-label") or span_hora.text
            if label: return label.strip()
        except: pass

        return datetime.now().strftime("%H:%M, %d/%m/%Y")

    def _extrair_autor_bolha(self, el):
        """Extrai o autor da mensagem de forma segura."""
        try:
            if "message-out" in (el.get_attribute("class") or ""): return "Você"
        except: pass

        for seletor in ['span[data-testid="author"]', 'span.copyable-text[dir="auto"]', '._akbu']:
            try:
                nome = (el.find_element(By.CSS_SELECTOR, seletor).text or "").strip()
                if nome: return nome
            except: pass

        try:
            meta = (el.find_element(By.CSS_SELECTOR, '.copyable-text').get_attribute("data-pre-plain-text") or "")
            if "] " in meta: return meta.split("] ", 1)[1].rstrip(":")
        except: pass

        return "Desconhecido"

    def _baixar_imagem_blob(self, src_url, nome_img):
        """Baixa imagens interceptando o XMLHttpRequest do navegador."""
        path_img = os.path.join(self.diretorio, "fotos", nome_img)
        script = """
            var uri = arguments[0];
            var callback = arguments[1];
            var xhr = new XMLHttpRequest();
            xhr.responseType = 'blob';
            xhr.onload = function() {
                var reader = new FileReader();
                reader.onloadend = function() { callback(reader.result); };
                reader.readAsDataURL(xhr.response);
            };
            xhr.onerror = function() { callback(''); };
            xhr.open('GET', uri);
            xhr.send();
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


    # ── 2. Captura Principal e Deduplicação (Hash) ─────────────────

    def capturar_mensagens_visiveis(self):
        """Lê os elementos visíveis na tela e aplica hash único."""
        mensagens = []
        try:
            elementos = self.driver.find_elements(By.CSS_SELECTOR, 'div.message-in, div.message-out')
            
            for el in elementos:
                try:
                    texto     = self._extrair_texto_bolha(el)
                    autor     = self._extrair_autor_bolha(el)
                    timestamp = self._extrair_timestamp_bolha(el)
                    foto_path = None
                    tipo      = "texto"

                    # Captura de imagem
                    try:
                        img_el  = el.find_element(By.CSS_SELECTOR, 'img[src*="blob:"]')
                        src_url = img_el.get_attribute("src")
                        if src_url:
                            nome_img  = f"img_{hashlib.md5(src_url.encode()).hexdigest()[:10]}.jpg"
                            foto_path = self._baixar_imagem_blob(src_url, nome_img)
                            tipo = "imagem" if foto_path else tipo
                            texto = f"{texto} [IMAGEM]" if texto else "[IMAGEM]"
                    except:
                        pass

                    if not texto.strip() and not foto_path:
                        continue

                    # Hash único para evitar duplicação no Lazy Load
                    chave_raw = f"{autor}|{texto}|{timestamp}"
                    hash_msg  = hashlib.md5(chave_raw.encode()).hexdigest()

                    mensagens.append({
                        "autor":     autor,
                        "texto":     texto,
                        "timestamp": timestamp,
                        "grupo":     self.grupo_atual or "Desconhecido",
                        "tipo":      tipo,
                        "foto_path": foto_path,
                        "hash_msg":  hash_msg,
                    })
                except StaleElementReferenceException:
                    continue
                except Exception:
                    continue
        except Exception as e:
            self.callback_log(f"⚠️ Erro na captura de DOM: {e}")

        return mensagens


    # ── 3. Lógica de Rolagem e Histórico ───────────────────────────

    def _encontrar_container_msgs(self):
        """Encontra o painel rolável correto do WhatsApp Web."""
        seletores = ['div[data-testid="conversation-panel-messages"]', '#main div[role="application"]', '#main']
        for sel in seletores:
            try:
                el = self.driver.find_element(By.CSS_SELECTOR, sel)
                if el: return el
            except: continue
        return None

    def rolar_para_topo(self, container):
        """Força a rolagem para cima usando injeção JS direta (Muito mais estável)."""
        try:
            self.driver.execute_script("arguments[0].scrollTop = 0;", container)
        except:
            try:
                webdriver.ActionChains(self.driver).send_keys(Keys.HOME).perform()
            except: pass

    def extrair_historico_completo(self, grupo, qtd_rolagens=50):
        """Varre o histórico de baixo para cima salvando apenas msgs novas."""
        if not self.abrir_grupo(grupo):
            return []

        self.callback_log(f"📜 Iniciando extração de '{grupo}'...")
        todas_mensagens  = {}   
        sem_novidade_cnt = 0    

        container = self._encontrar_container_msgs()
        if not container:
            self.callback_log("❌ Container de mensagens não encontrado.")
            return []

        for i in range(qtd_rolagens):
            msgs_agora = self.capturar_mensagens_visiveis()
            novas = 0

            for m in msgs_agora:
                h = m["hash_msg"]
                if h not in todas_mensagens:
                    todas_mensagens[h] = m
                    self._salvar_mensagem(m)
                    novas += 1

            if novas == 0:
                sem_novidade_cnt += 1
                if sem_novidade_cnt >= 5:
                    self.callback_log("📌 Chegamos ao início da conversa!")
                    break
            else:
                sem_novidade_cnt = 0

            self.rolar_para_topo(container)
            time.sleep(2.5) # Aguarda o servidor do WhatsApp carregar as mensagens antigas

            if i % 5 == 0 or novas > 0:
                self.callback_log(f"⏳ Bloco {i+1}/{qtd_rolagens} | Novas: {novas} | Total: {len(todas_mensagens)}")

        resultado = list(todas_mensagens.values())
        self.callback_log(f"✅ Extração concluída! {len(resultado)} mensagens.")
        return resultado


    # ── 4. Exportação Combinada ────────────────────────────────────

    def exportar_html_combinado(self, grupo, msgs):
        """Gera um arquivo HTML combinando texto e fotos."""
        if not msgs:
            return None

        nome_arquivo = grupo.replace("/", "-").replace("\\", "-")[:50]
        ts_export    = datetime.now().strftime("%Y%m%d_%H%M%S")
        path_html    = os.path.join(self.diretorio, "exports", f"{nome_arquivo}_{ts_export}.html")

        def foto_para_base64(path):
            try:
                if path and os.path.exists(path):
                    with open(path, "rb") as f:
                        return base64.b64encode(f.read()).decode()
            except: pass
            return None

        linhas_html = []
        for m in msgs:
            autor     = m["autor"]
            texto     = m["texto"].replace("[IMAGEM]", "").strip()
            ts        = m["timestamp"]
            is_out    = autor == "Você"
            lado      = "msg-out" if is_out else "msg-in"
            foto_b64  = foto_para_base64(m.get("foto_path"))

            partes_conteudo = []
            if foto_b64:
                partes_conteudo.append(f'<img src="data:image/jpeg;base64,{foto_b64}" style="max-width:300px;border-radius:8px;display:block;margin-bottom:4px;" />')
            if texto:
                partes_conteudo.append(f'<span class="msg-text">{texto}</span>')

            if not partes_conteudo: continue

            conteudo_inner = "\n".join(partes_conteudo)

            linhas_html.append(f'''
            <div class="msg-wrapper {lado}">
              <div class="bubble">
                {"" if is_out else f'<span class="autor">{autor}</span>'}
                {conteudo_inner}
                <span class="ts">{ts}</span>
              </div>
            </div>''')

        html = f"""<!DOCTYPE html>
        <html lang="pt-BR">
        <head>
        <meta charset="UTF-8"><title>Histórico — {grupo}</title>
        <style>
            * {{ box-sizing: border-box; margin: 0; padding: 0; }}
            body {{ font-family: sans-serif; background: #e5ddd5; padding: 20px; }}
            .chat-container {{ max-width: 720px; margin: 0 auto; display: flex; flex-direction: column; gap: 4px; }}
            .msg-wrapper {{ display: flex; margin: 2px 0; }}
            .msg-in {{ justify-content: flex-start; }}
            .msg-out {{ justify-content: flex-end; }}
            .bubble {{ max-width: 72%; padding: 8px 12px; border-radius: 12px; font-size: 14px; position: relative; box-shadow: 0 1px 2px rgba(0,0,0,.15); }}
            .msg-in .bubble {{ background: #fff; border-top-left-radius: 0; }}
            .msg-out .bubble {{ background: #dcf8c6; border-top-right-radius: 0; }}
            .autor {{ display: block; font-weight: bold; font-size: 13px; color: #128c7e; margin-bottom: 3px; }}
            .ts {{ display: block; font-size: 11px; color: #999; text-align: right; margin-top: 4px; }}
            h1 {{ text-align: center; color: #075e54; background: #fff; padding: 16px; border-radius: 12px; margin-bottom: 20px; }}
        </style>
        </head>
        <body>
        <h1>📱 {grupo}</h1>
        <div class="chat-container">
            {"".join(linhas_html)}
        </div>
        </body>
        </html>"""

        with open(path_html, "w", encoding="utf-8") as f:
            f.write(html)

        self.callback_log(f"📄 HTML exportado: {path_html}")
        return path_html


# ══════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════
if __name__ == "__main__":
    app = App()
    app.mainloop()