import io
import time
import pandas as pd
import streamlit as st
from openai import OpenAI
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

# --- Configuración Visual ---
st.set_page_config(
    page_title="MethaneZero-AI",
    page_icon="🌱",
    layout="wide"
)

# --- Constantes y Parámetros del Modelo ---
FACTOR_METANO = 28
TEMP_OPTIMA = 4.0
K_DETERIORO = 0.10
UMBRAL_CRITICO = 0.1
UMBRAL_DONACION = 6.0
UMBRAL_DESCUENTO = 18.0
TASA_DEDUCCION = 0.25
TASA_DESCUENTO = 0.50

CANAL_DONACION = "Donación (Banco Alimentos)"
CANAL_DESCUENTO = "Descuento en Barra (50%)"
CANAL_NORMAL = "Vitrina Normal"
CANAL_COMPOSTAJE = "Compostaje / Merma"
CANALES_DESVIO = [CANAL_DONACION, CANAL_DESCUENTO]

# Factores de categoría en kg CH4 por kg de alimento (calibrados con factor 0,042
# para que el promedio de la hipótesis coincida con EPA WARM, ~1,79 kg CO2e/kg)
FACTORES_EMISION = {
    "Cárnicos y Embutidos": 0.105,
    "Almuerzos y Comidas Preparadas": 0.092,
    "Sándwiches y Comidas Rápidas": 0.084,
    "Lácteos y Derivados": 0.080,
    "Frituras y Snacks Calientes": 0.067,
    "Repostería y Pan Dulce": 0.050,
    "Panadería Tradicional": 0.038,
    "Frutas Frescas Porcionadas": 0.034,
    "Jugos y Bebidas Naturales": 0.029,
    "Ensaladas y Vegetales": 0.021
}


# --- Algoritmos y Motor Cinético-Térmico ---
def calcular_horas_reales(horas_refrigeradas, temp_real, temp_optima=TEMP_OPTIMA):
    if temp_real < 0:
        return 0.0
    if temp_real <= temp_optima:
        return horas_refrigeradas
    factor_deterioro = 1.0 + (temp_real - temp_optima) * K_DETERIORO
    return round(horas_refrigeradas / factor_deterioro, 2)


def calcular_criticidad(producto):
    categoria = producto["categoria"]
    peso = producto["peso_kg"]
    horas_nevera = producto["horas_vencimiento"]
    temp_actual = producto["temp_actual"]

    horas_reales = calcular_horas_reales(horas_nevera, temp_actual)
    producto["horas_reales"] = horas_reales

    factor_cat = FACTORES_EMISION.get(categoria, 1.0)
    iepm = peso * factor_cat * FACTOR_METANO
    producto["iepm"] = round(iepm, 3)

    if horas_reales <= UMBRAL_CRITICO:
        criticidad = 999999.0
    else:
        criticidad = iepm / horas_reales

    producto["criticidad"] = round(criticidad, 4)
    return producto


def mezclar(izquierda, derecha):
    resultado = []
    i = 0
    j = 0
    while i < len(izquierda) and j < len(derecha):
        if izquierda[i]["criticidad"] >= derecha[j]["criticidad"]:
            resultado.append(izquierda[i])
            i += 1
        else:
            resultado.append(derecha[j])
            j += 1
    resultado.extend(izquierda[i:])
    resultado.extend(derecha[j:])
    return resultado


def merge_sort_criticidad(lista_productos):
    if len(lista_productos) <= 1:
        return lista_productos
    medio = len(lista_productos) // 2
    izq_ordenada = merge_sort_criticidad(lista_productos[:medio])
    der_ordenada = merge_sort_criticidad(lista_productos[medio:])
    return mezclar(izq_ordenada, der_ordenada)


def insertar_alerta_vitrina(lista_ordenada, producto_alerta):
    producto_alerta = calcular_criticidad(producto_alerta)
    if any(p["id"] == producto_alerta["id"] for p in lista_ordenada):
        producto_alerta["id"] = max(p["id"] for p in lista_ordenada) + 1

    lista_ordenada.append(producto_alerta)
    i = len(lista_ordenada) - 1

    while i > 0 and lista_ordenada[i]["criticidad"] > lista_ordenada[i - 1]["criticidad"]:
        lista_ordenada[i], lista_ordenada[i - 1] = lista_ordenada[i - 1], lista_ordenada[i]
        i -= 1

    return lista_ordenada


def enrutar_producto(producto):
    horas = producto.get("horas_reales", producto["horas_vencimiento"])
    precio = producto["precio_comercial"]
    producto["beneficio_fiscal"] = 0.0
    producto["ingreso_estimado"] = 0.0

    if horas <= UMBRAL_CRITICO:
        producto["canal"] = CANAL_COMPOSTAJE
    elif horas <= UMBRAL_DONACION:
        producto["canal"] = CANAL_DONACION
        producto["beneficio_fiscal"] = round(precio * TASA_DEDUCCION, 2)
    elif horas <= UMBRAL_DESCUENTO:
        producto["canal"] = CANAL_DESCUENTO
        producto["ingreso_estimado"] = round(precio * TASA_DESCUENTO, 2)
    else:
        producto["canal"] = CANAL_NORMAL

    return producto


# --- Limpieza de Caracteres Unicode no soportados por Helvetica en PDF ---
def limpiar_texto_pdf(texto):
    reemplazos = {
        "₀": "0", "₁": "1", "₂": "2", "₃": "3", "₄": "4",
        "₅": "5", "₆": "6", "₇": "7", "₈": "8", "₉": "9",
        "—": "-", "–": "-", "“": '"', "”": '"', "‘": "'", "’": "'"
    }
    for origen, destino in reemplazos.items():
        texto = texto.replace(origen, destino)
    return texto


# --- Generador de PDF Dinámico Multipágina (ReportLab) ---
def generar_pdf_informe(lote_procesado, kpis, texto_ia):
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        rightMargin=36,
        leftMargin=36,
        topMargin=36,
        bottomMargin=36
    )

    estilos = getSampleStyleSheet()
    color_primario = colors.HexColor("#1A365D")
    color_secundario = colors.HexColor("#2B6CB0")

    estilo_titulo = ParagraphStyle(
        "TituloDoc",
        parent=estilos["Heading1"],
        fontName="Helvetica-Bold",
        fontSize=16,
        leading=20,
        textColor=color_primario
    )
    estilo_subtitulo = ParagraphStyle(
        "SubtituloDoc",
        parent=estilos["Normal"],
        fontName="Helvetica",
        fontSize=9,
        leading=13,
        textColor=colors.HexColor("#4A5568")
    )
    estilo_seccion = ParagraphStyle(
        "SeccionDoc",
        parent=estilos["Heading2"],
        fontName="Helvetica-Bold",
        fontSize=11,
        leading=15,
        textColor=color_secundario,
        spaceBefore=8,
        spaceAfter=4
    )
    estilo_cuerpo = ParagraphStyle(
        "CuerpoDoc",
        parent=estilos["Normal"],
        fontName="Helvetica",
        fontSize=8.5,
        leading=12,
        textColor=colors.HexColor("#2D3748")
    )
    estilo_tabla = ParagraphStyle(
        "TablaTexto",
        parent=estilos["Normal"],
        fontName="Helvetica",
        fontSize=7.5,
        leading=9.5,
        textColor=colors.black
    )
    estilo_tabla_centro = ParagraphStyle(
        "TablaTextoCentro",
        parent=estilo_tabla,
        alignment=TA_CENTER
    )
    estilo_tabla_bold = ParagraphStyle(
        "TablaTextoBold",
        parent=estilo_tabla,
        fontName="Helvetica-Bold"
    )
    estilo_tabla_header = ParagraphStyle(
        "TablaHeader",
        parent=estilo_tabla,
        fontName="Helvetica-Bold",
        textColor=colors.white
    )
    estilo_tabla_header_centro = ParagraphStyle(
        "TablaHeaderCentro",
        parent=estilo_tabla_header,
        alignment=TA_CENTER
    )

    story = []

    # Encabezado principal
    story.append(Paragraph("METHANEZERO-AI: INFORME DE TURNO Y SOSTENIBILIDAD", estilo_titulo))
    story.append(Paragraph(
        f"Fecha de emisión: {time.strftime('%d/%m/%Y %H:%M')} | Establecimiento: Cafetería Universitaria | Campus Julio Garavito",
        estilo_subtitulo
    ))
    story.append(Spacer(1, 6))
    story.append(HRFlowable(width="100%", thickness=1.5, color=color_primario, spaceAfter=8))

    # 1. Resumen de KPIs
    story.append(Paragraph("1. BALANCE CONSOLIDADO DE IMPACTO (ODS 13 & LEY 1990 DE 2019)", estilo_seccion))
    datos_kpis = [
        [
            Paragraph("<b>Métrica Operativa / Ambiental</b>", estilo_tabla_bold),
            Paragraph("<b>Valor Consolidado</b>", estilo_tabla_bold),
            Paragraph("<b>Impacto / Destino</b>", estilo_tabla_bold)
        ],
        [
            Paragraph("Emisiones de Metano Desviadas (ODS 13)", estilo_tabla),
            Paragraph(f"{kpis['iepm_mitigado']:.2f} kg CO2e", estilo_tabla_bold),
            Paragraph(f"{kpis['pct_desvio']:.1f}% del potencial total mitigado", estilo_tabla)
        ],
        [
            Paragraph("Escudo Fiscal Recuperado (Ley 1990 / Art. 257 E.T.)", estilo_tabla),
            Paragraph(f"${kpis['escudo_fiscal']:,.0f} COP", estilo_tabla_bold),
            Paragraph("Descuento del 25% en el impuesto de renta por donación", estilo_tabla)
        ],
        [
            Paragraph("Ingreso Recuperado por Remate en Barra", estilo_tabla),
            Paragraph(f"${kpis['ingreso_recuperado']:,.0f} COP", estilo_tabla_bold),
            Paragraph("Canal de rotación rápida al 50% de descuento", estilo_tabla)
        ],
        [
            Paragraph("Pérdida Económica por Descarte / Vencimiento", estilo_tabla),
            Paragraph(f"${kpis['perdida_merma']:,.0f} COP", estilo_tabla_bold),
            Paragraph("Desviado a compostaje aeróbico (sin metano)", estilo_tabla)
        ]
    ]

    t_kpis = Table(datos_kpis, colWidths=[200, 110, 230])
    t_kpis.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EDF2F7")),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E0")),
    ]))
    story.append(t_kpis)
    story.append(Spacer(1, 8))

    # 2. Análisis IA (con limpieza de subíndices Unicode para evitar cuadros negros)
    story.append(Paragraph("2. DICTAMEN EJECUTIVO DE SOSTENIBILIDAD (SÍNTESIS IA)", estilo_seccion))
    texto_informe = texto_ia if texto_ia else (
        "El turno concluyó con una mitigación efectiva de metano. Se priorizó el enrutamiento de alimentos perecederos "
        "con vida útil crítica hacia los bancos de alimentos, consolidando el beneficio tributario del 25% y evitando "
        "la disposición final anaeróbica en vertederos."
    )
    texto_informe_limpio = limpiar_texto_pdf(texto_informe)

    for parrafo in texto_informe_limpio.split("\n\n"):
        if parrafo.strip():
            story.append(Paragraph(parrafo.replace("\n", " "), estilo_cuerpo))
            story.append(Spacer(1, 3))

    story.append(Spacer(1, 6))

    # 3. Tabla Completa de Lotes
    story.append(Paragraph(f"3. DETALLE DE PRODUCTOS AUDITADOS EN VITRINA ({len(lote_procesado)} REGISTROS)", estilo_seccion))
    encabezados_lotes = [
        Paragraph("<b>#</b>", estilo_tabla_header_centro),
        Paragraph("<b>Producto / Lote</b>", estilo_tabla_header),
        Paragraph("<b>Categoría</b>", estilo_tabla_header),
        Paragraph("<b>Peso</b>", estilo_tabla_header),
        Paragraph("<b>Horas</b>", estilo_tabla_header),
        Paragraph("<b>IEPM (kg)</b>", estilo_tabla_header),
        Paragraph("<b>Canal Asignado</b>", estilo_tabla_header),
        Paragraph("<b>Beneficio</b>", estilo_tabla_header)
    ]
    tabla_lotes_data = [encabezados_lotes]

    for idx, p in enumerate(lote_procesado, 1):
        canal_corto = p["canal"].split("(")[0].strip()
        tabla_lotes_data.append([
            Paragraph(str(idx), estilo_tabla_centro),
            Paragraph(limpiar_texto_pdf(p["nombre"][:30]), estilo_tabla),
            Paragraph(limpiar_texto_pdf(p["categoria"][:17]), estilo_tabla),
            Paragraph(f"{p['peso_kg']} kg", estilo_tabla),
            Paragraph(f"{p['horas_reales']} h", estilo_tabla),
            Paragraph(f"{p['iepm']:.2f}", estilo_tabla),
            Paragraph(canal_corto, estilo_tabla_bold),
            Paragraph(f"${p['beneficio_fiscal']:,.0f}", estilo_tabla)
        ])

    t_lotes = Table(tabla_lotes_data, colWidths=[28, 140, 92, 42, 38, 48, 97, 55], repeatRows=1)
    t_lotes.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2B6CB0")),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
        ("TOPPADDING", (0, 0), (-1, -1), 2.5),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#E2E8F0")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F7FAFC")]),
    ]))
    story.append(t_lotes)

    story.append(Spacer(1, 10))
    story.append(HRFlowable(width="100%", thickness=0.8, color=colors.HexColor("#A0AEC0"), spaceAfter=6))
    story.append(Paragraph(
        "Certificación: Sistema MethaneZero-AI | Conforme a la Ley 1990 de 2019 y ODS 13 | Algoritmos: Merge Sort Θ(n log n) & Insertion Sort O(n)",
        ParagraphStyle("Pie", parent=estilos["Normal"], fontName="Helvetica-Oblique", fontSize=7, textColor=colors.HexColor("#718096"))
    ))

    doc.build(story)
    buffer.seek(0)
    return buffer.getvalue()


# --- Datos de la Hipótesis (305 unidades en 15 lotes) ---
def obtener_hipotesis_300():
    datos = [
        {"nombre": "Papas rellenas del turno mañana", "categoria": "Frituras y Snacks Calientes", "cantidad": 15, "peso_unit": 0.18, "precio_unit": 4000, "horas": 0.0, "temp": 22.0, "vencido": True},
        {"nombre": "Bandeja ejecutiva carne asada", "categoria": "Almuerzos y Comidas Preparadas", "cantidad": 10, "peso_unit": 0.50, "precio_unit": 16000, "horas": 0.0, "temp": 20.0, "vencido": True},
        {"nombre": "Arroz con pollo del mediodía", "categoria": "Almuerzos y Comidas Preparadas", "cantidad": 15, "peso_unit": 0.45, "precio_unit": 14000, "horas": 1.5, "temp": 25.0, "vencido": False},
        {"nombre": "Empanadas de carne y pollo", "categoria": "Frituras y Snacks Calientes", "cantidad": 25, "peso_unit": 0.12, "precio_unit": 3500, "horas": 2.5, "temp": 24.0, "vencido": False},
        {"nombre": "Vaso fruta picada (papaya/melón)", "categoria": "Frutas Frescas Porcionadas", "cantidad": 25, "peso_unit": 0.30, "precio_unit": 4500, "horas": 3.0, "temp": 12.0, "vencido": False},
        {"nombre": "Sándwich pollo con tártara", "categoria": "Sándwiches y Comidas Rápidas", "cantidad": 20, "peso_unit": 0.25, "precio_unit": 8500, "horas": 4.0, "temp": 8.0, "vencido": False},
        {"nombre": "Sándwich mixto jamón y queso", "categoria": "Sándwiches y Comidas Rápidas", "cantidad": 25, "peso_unit": 0.22, "precio_unit": 7500, "horas": 5.0, "temp": 6.0, "vencido": False},
        {"nombre": "Ensalada César con pollo", "categoria": "Ensaladas y Vegetales", "cantidad": 10, "peso_unit": 0.25, "precio_unit": 9500, "horas": 5.5, "temp": 9.0, "vencido": False},
        {"nombre": "Jugo de naranja natural (botella)", "categoria": "Jugos y Bebidas Naturales", "cantidad": 20, "peso_unit": 0.35, "precio_unit": 5000, "horas": 8.0, "temp": 7.0, "vencido": False},
        {"nombre": "Vaso fruta fresca (piña/mango)", "categoria": "Frutas Frescas Porcionadas", "cantidad": 20, "peso_unit": 0.30, "precio_unit": 4500, "horas": 10.0, "temp": 4.0, "vencido": False},
        {"nombre": "Yogures en vaso surtidos", "categoria": "Lácteos y Derivados", "cantidad": 35, "peso_unit": 0.15, "precio_unit": 3200, "horas": 14.0, "temp": 4.0, "vencido": False},
        {"nombre": "Porción torta 3 leches", "categoria": "Repostería y Pan Dulce", "cantidad": 15, "peso_unit": 0.20, "precio_unit": 7000, "horas": 16.0, "temp": 5.0, "vencido": False},
        {"nombre": "Bolsas leche entera 1L", "categoria": "Lácteos y Derivados", "cantidad": 15, "peso_unit": 1.00, "precio_unit": 4800, "horas": 18.0, "temp": 4.0, "vencido": False},
        {"nombre": "Donas glaseadas de vitrina", "categoria": "Repostería y Pan Dulce", "cantidad": 20, "peso_unit": 0.10, "precio_unit": 3500, "horas": 22.0, "temp": 18.0, "vencido": False},
        {"nombre": "Pandebonos y croissants", "categoria": "Panadería Tradicional", "cantidad": 35, "peso_unit": 0.08, "precio_unit": 2500, "horas": 28.0, "temp": 20.0, "vencido": False}
    ]

    lotes_generados = []
    for i, item in enumerate(datos, 1):
        p_total = round(item["peso_unit"] * item["cantidad"], 3)
        v_total = int(item["precio_unit"] * item["cantidad"])
        h_final = 0.0 if item["vencido"] else item["horas"]

        lote = {
            "id": i,
            "nombre": f"Lote ({item['cantidad']}x) {item['nombre']}",
            "categoria": item["categoria"],
            "es_lote": True,
            "cantidad": item["cantidad"],
            "peso_kg": p_total,
            "horas_vencimiento": h_final,
            "temp_actual": item["temp"],
            "precio_comercial": v_total
        }
        lotes_generados.append(calcular_criticidad(lote))
    return lotes_generados


# --- Cliente de LM Studio (Inferencia Local) ---
def consultar_lm_studio(mensajes):
    try:
        cliente = OpenAI(base_url="http://localhost:1234/v1", api_key="lm-studio", timeout=180)
        lista_modelos = cliente.models.list()
        ids_chat = [m.id for m in lista_modelos.data if "embed" not in m.id.lower()]
        modelo_activo = ids_chat[0] if ids_chat else "local-model"

        respuesta = cliente.chat.completions.create(
            model=modelo_activo,
            messages=mensajes,
            temperature=0.3
        )
        return respuesta.choices[0].message.content
    except Exception:
        return None


# --- Inicialización del Estado Global ---
if "pantalla" not in st.session_state:
    st.session_state.pantalla = "inicio"

if "inventario" not in st.session_state:
    st.session_state.inventario = []

if "reporte_ia" not in st.session_state:
    st.session_state.reporte_ia = ""

if "mensajes_chat" not in st.session_state:
    st.session_state.mensajes_chat = [
        {"role": "assistant", "content": "Hola. Soy el asistente de sostenibilidad de la cafetería. Pregúntame sobre el estado de la vitrina, el beneficio de la Ley 1990 o por qué se priorizó un lote en particular."}
    ]


# ========================================================
# PANTALLA 1: BIENVENIDA Y PROPÓSITO
# ========================================================
if st.session_state.pantalla == "inicio":
    st.title("🌱 MethaneZero-AI")
    st.subheader("Sistema Inteligente de Gestión de Vitrina y Desvío de Emisiones de Metano")

    st.markdown("""
    Esta plataforma optimiza la rotación de alimentos perecederos en la cafetería universitaria para evitar el desperdicio orgánico.

    ### Puntos clave del sistema:
    1. **Impacto Ambiental (ODS 13):** Cuando la comida se descompone en un vertedero sin oxígeno, genera **gas metano ($CH_4$)**, que tiene un potencial de calentamiento 28 veces mayor al $CO_2$. Priorizamos su despacho oportuno.
    2. **Arquitectura Algorítmica:**
       * **Merge Sort $\\Theta(n \\log n)$:** Clasifica el inventario completo de apertura de mayor a menor criticidad ambiental de forma estable.
       * **Insertion Sort $\\mathcal{O}(n)$ in-place:** Si un lote sufre elevación térmica durante el servicio, se reubica instantáneamente sin recalcular toda la vitrina.
    3. **Beneficio Tributario (Ley 1990 de 2019 / Art. 257 E.T.):** Lotes con vida útil crítica se canalizan a donación certificada, capturando un **25% de su valor comercial** como descuento directo en el impuesto de renta.
    """)

    st.markdown("---")
    if st.button("🚀 Iniciar Turno y Cargar Productos", type="primary"):
        st.session_state.pantalla = "registro"
        st.rerun()


# ========================================================
# PANTALLA 2: REGISTRO MATUTINO (INVENTARIO EN BLANCO)
# ========================================================
elif st.session_state.pantalla == "registro":
    st.title("📦 Registro de Apertura Matutina")
    st.caption("Ingrese los productos manualmente o cargue la hipótesis experimental de 305 unidades.")

    col_hip1, col_hip2 = st.columns([2, 1])
    with col_hip1:
        if st.button("⚡ Cargar Hipótesis Experimental (305 unidades en 15 lotes)", type="secondary", use_container_width=True):
            st.session_state.inventario = obtener_hipotesis_300()
            st.toast("Cargados 15 lotes con 305 unidades representativas de cafetería.", icon="📦")
            st.rerun()
    with col_hip2:
        if len(st.session_state.inventario) > 0:
            if st.button("🗑️ Vaciar Todo", use_container_width=True):
                st.session_state.inventario = []
                st.rerun()

    st.markdown("---")

    with st.form("form_registro_apertura", clear_on_submit=True):
        st.subheader("Ficha del Producto o Lote")
        col_izq, col_der = st.columns(2)

        with col_izq:
            nombre_input = st.text_input("Nombre del producto:", placeholder="Ej: Sándwich artesanal de pollo, Yogurt con mora")
            cat_seleccionada = st.selectbox("Categoría bromatológica:", list(FACTORES_EMISION.keys()))
            unidades = st.number_input("Cantidad de unidades (deja 1 si es un solo producto):", min_value=1, max_value=500, value=1, step=1)
            peso_unit = st.number_input("Peso unitario de 1 producto (kg):", min_value=0.01, max_value=20.0, value=0.20, step=0.05)

        with col_der:
            precio_unit = st.number_input("Precio de venta de 1 producto ($ COP):", min_value=500, max_value=200000, value=4000, step=500)
            horas_input = st.number_input("Horas de vida útil estimadas en frío:", min_value=0.1, max_value=720.0, value=12.0, step=0.5)
            temp_llegada = st.number_input("Temperatura actual con la que ingresa (°C):", min_value=-5.0, max_value=30.0, value=4.0, step=0.5)
            esta_vencido = st.checkbox("⚠️ Marcar como vencido o dañado (envío directo a compostaje)", value=False)

        btn_agregar = st.form_submit_button("➕ Agregar a la Vitrina", use_container_width=True)

        if btn_agregar:
            nombre_limpio = nombre_input.strip()
            if len(nombre_limpio) == 0:
                st.error("❌ Por favor ingrese el nombre del producto.")
            else:
                horas_finales = 0.0 if esta_vencido else horas_input
                es_lote = unidades > 1
                peso_total = round(peso_unit * unidades, 3)
                precio_total = int(precio_unit * unidades)
                nombre_final = f"Lote ({unidades}x) {nombre_limpio}" if es_lote else nombre_limpio

                nuevo_id = max([p["id"] for p in st.session_state.inventario], default=0) + 1

                nuevo_item = {
                    "id": nuevo_id,
                    "nombre": nombre_final,
                    "categoria": cat_seleccionada,
                    "es_lote": es_lote,
                    "cantidad": unidades,
                    "peso_kg": peso_total,
                    "horas_vencimiento": horas_finales,
                    "temp_actual": temp_llegada,
                    "precio_comercial": precio_total
                }

                nuevo_item = calcular_criticidad(nuevo_item)
                st.session_state.inventario.append(nuevo_item)
                st.toast(f"Agregado: {nombre_final}", icon="✅")

    st.markdown("---")
    st.subheader(f"Productos listos para apertura ({len(st.session_state.inventario)})")

    if len(st.session_state.inventario) == 0:
        st.info("La vitrina está vacía. Ingrese productos arriba o presione el botón de hipótesis experimental.")
    else:
        filas_previas = []
        for p in st.session_state.inventario:
            filas_previas.append({
                "ID": p["id"],
                "Producto": p["nombre"],
                "Categoría": p["categoria"],
                "Cant.": p["cantidad"],
                "Peso Total (kg)": p["peso_kg"],
                "Temp (°C)": p["temp_actual"],
                "Horas en Frío": p["horas_vencimiento"],
                "Precio Total": f"${p['precio_comercial']:,.0f}"
            })

        st.dataframe(pd.DataFrame(filas_previas), use_container_width=True, hide_index=True)

        with st.expander("🗑️ Eliminar un producto específico por error de ingreso"):
            col_sel, col_btn_del = st.columns([3, 1])
            with col_sel:
                opciones_borrar = {f"ID {p['id']} - {p['nombre']} (${p['precio_comercial']:,})": p['id'] for p in st.session_state.inventario}
                item_seleccionado = st.selectbox("Seleccione el producto a descartar:", list(opciones_borrar.keys()), key="sel_eliminar_apertura")
            with col_btn_del:
                st.write("")
                st.write("")
                if st.button("Eliminar Seleccionado", use_container_width=True):
                    id_a_remover = opciones_borrar[item_seleccionado]
                    st.session_state.inventario = [p for p in st.session_state.inventario if p["id"] != id_a_remover]
                    st.toast("Producto eliminado del registro.", icon="🗑️")
                    st.rerun()

        st.markdown("---")
        if st.button("🚀 Abrir Vitrina y Ejecutar Merge Sort", type="primary", use_container_width=True):
            st.session_state.inventario = merge_sort_criticidad(st.session_state.inventario)
            st.session_state.pantalla = "vitrina"
            st.rerun()


# ========================================================
# PANTALLA 3: VITRINA ACTIVA, RESUMEN IA & DESCARGA PDF
# ========================================================
elif st.session_state.pantalla == "vitrina":
    st.title("🏪 Vitrina en Operación")
    st.caption("Inventario priorizado mediante Merge Sort. Registre choques térmicos en la barra lateral.")

    with st.sidebar:
        st.header("🚨 Registrar Alerta Térmica")
        st.caption("Simula un producto o lote olvidado fuera de refrigeración.")

        with st.form("form_alerta_piso", clear_on_submit=True):
            nom_alt = st.text_input("Producto afectado:", placeholder="Ej: Sándwich olvidado en barra")
            cat_alt = st.selectbox("Categoría:", list(FACTORES_EMISION.keys()))
            cant_alt = st.number_input("Cantidad de unidades:", min_value=1, max_value=200, value=1, step=1)
            peso_alt = st.number_input("Peso unitario (kg):", min_value=0.01, max_value=20.0, value=0.25)
            prec_alt = st.number_input("Precio unitario ($):", min_value=500, max_value=200000, value=8000, step=500)
            horas_alt = st.number_input("Horas que le quedaban en frío:", min_value=0.1, max_value=720.0, value=6.0)
            temp_alt = st.slider("Temperatura a la que subió (°C):", min_value=4.0, max_value=40.0, value=24.0, step=0.5)
            marcar_daniado = st.checkbox("⚠️ El producto ya se dañó / agrió", value=False)

            btn_alerta = st.form_submit_button("⚡ Reubicar con Insertion Sort", use_container_width=True)

            if btn_alerta:
                if len(nom_alt.strip()) == 0:
                    st.error("Ingrese el nombre del producto.")
                else:
                    horas_reales_alt = 0.0 if marcar_daniado else horas_alt
                    es_lote_alt = cant_alt > 1
                    nuevo_id = max([p["id"] for p in st.session_state.inventario], default=0) + 1

                    item_alerta = {
                        "id": nuevo_id,
                        "nombre": f"Lote ({cant_alt}x) {nom_alt.strip()}" if es_lote_alt else nom_alt.strip(),
                        "categoria": cat_alt,
                        "es_lote": es_lote_alt,
                        "cantidad": cant_alt,
                        "peso_kg": round(peso_alt * cant_alt, 3),
                        "horas_vencimiento": horas_reales_alt,
                        "temp_actual": temp_alt,
                        "precio_comercial": int(prec_alt * cant_alt)
                    }
                    st.session_state.inventario = insertar_alerta_vitrina(st.session_state.inventario, item_alerta)
                    st.toast("Producto reubicado con Insertion Sort.", icon="⚡")
                    st.rerun()

        st.markdown("---")
        if st.button("⬅️ Volver a Cargar Inventario"):
            st.session_state.pantalla = "registro"
            st.rerun()

    # Balance y Tarjetas de Métricas
    lote_procesado = [enrutar_producto(dict(p)) for p in st.session_state.inventario]
    iepm_mitigado = sum(p["iepm"] for p in lote_procesado if p["canal"] in CANALES_DESVIO)
    iepm_potencial = sum(p["iepm"] for p in lote_procesado)
    escudo_fiscal = sum(p["beneficio_fiscal"] for p in lote_procesado)
    ingreso_recuperado = sum(p["ingreso_estimado"] for p in lote_procesado)
    perdida_merma = sum(p["precio_comercial"] for p in lote_procesado if p["canal"] == CANAL_COMPOSTAJE)
    pct_desvio = (iepm_mitigado / iepm_potencial * 100) if iepm_potencial > 0 else 0.0

    kpis_dict = {
        "iepm_mitigado": iepm_mitigado,
        "iepm_potencial": iepm_potencial,
        "escudo_fiscal": escudo_fiscal,
        "ingreso_recuperado": ingreso_recuperado,
        "perdida_merma": perdida_merma,
        "pct_desvio": pct_desvio
    }

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Mitigación Metano (ODS 13)", f"{iepm_mitigado:.2f} kg CO2e", f"{pct_desvio:.1f}% desvío")
    c2.metric("Escudo Fiscal (Ley 1990)", f"${escudo_fiscal:,.0f} COP", "25% descuento en impuesto de renta")
    c3.metric("Recuperación por Remate", f"${ingreso_recuperado:,.0f} COP", "Descuento 50%")
    c4.metric("Pérdida por Descarte", f"${perdida_merma:,.0f} COP", "Compostaje", delta_color="inverse")

    st.markdown("---")

    # Pestañas de Navegación Operativa
    tab_tabla, tab_reporte_ia, tab_chatbot = st.tabs([
        "📋 Vitrina Clasificada",
        "🤖 Reporte Ejecutivo con IA",
        "💬 Chatbot de Consultas"
    ])

    with tab_tabla:
        st.subheader("Prioridad de Despacho en Vitrina (Orden Descendente)")
        filas_vitrina = []
        for item in lote_procesado:
            filas_vitrina.append({
                "Prioridad": len(filas_vitrina) + 1,
                "ID": item["id"],
                "Producto": item["nombre"],
                "Categoría": item["categoria"],
                "Temp (°C)": item["temp_actual"],
                "Horas Reales Útiles": item["horas_reales"],
                "Metano Potencial (kg CO2e)": item["iepm"],
                "Índice Criticidad": item["criticidad"],
                "Destino Sugerido": item["canal"],
                "Valor Total": f"${item['precio_comercial']:,.0f}",
                "Beneficio Tributario": f"${item['beneficio_fiscal']:,.0f}"
            })

        st.dataframe(pd.DataFrame(filas_vitrina), use_container_width=True, hide_index=True)

        if len(st.session_state.inventario) > 0:
            with st.expander("📦 Despachar o Retirar un Producto de la Vitrina"):
                col_v1, col_v2 = st.columns([3, 1])
                with col_v1:
                    opciones_vitrina = {f"ID {p['id']}: {p['nombre']} (Canal: {enrutar_producto(dict(p))['canal']})": p['id'] for p in st.session_state.inventario}
                    prod_a_retirar = st.selectbox("Seleccione el producto a retirar:", list(opciones_vitrina.keys()), key="sel_retirar_vitrina")
                with col_v2:
                    st.write("")
                    st.write("")
                    if st.button("Retirar de Vitrina", use_container_width=True):
                        id_retirar = opciones_vitrina[prod_a_retirar]
                        st.session_state.inventario = [p for p in st.session_state.inventario if p["id"] != id_retirar]
                        st.toast("Producto retirado de la vitrina.", icon="📤")
                        st.rerun()

    with tab_reporte_ia:
        st.subheader("Generación de Informe Ejecutivo de Sostenibilidad")
        st.caption("El modelo local en LM Studio sintetiza el balance ambiental y tributario de la jornada.")

        col_rep1, col_rep2 = st.columns([1, 1])
        with col_rep1:
            btn_redactar = st.button("⚡ Redactar Informe Ejecutivo", type="primary", use_container_width=True)

        if btn_redactar:
            if len(st.session_state.inventario) == 0:
                st.warning("No hay productos en el inventario para analizar.")
            else:
                with st.spinner("Conectando con LM Studio y redactando análisis ejecutivo..."):
                    resumen_lotes = ", ".join([f"{p['nombre']} ({p['canal']})" for p in lote_procesado[:10]])
                    prompt_sistema = (
                        "Eres un consultor experto en sostenibilidad y tributaria en Colombia. "
                        "Redacta un informe ejecutivo conciso, formal y estructurado para la administración de la cafetería universitaria. "
                        "Destaca los logros del ODS 13 (metano mitigado) y la recuperación financiera por la Ley 1990 de 2019 (descuento tributario del 25% sobre el valor donado, Art. 257 E.T.). "
                        "Usa texto plano formal sin comandos de LaTeX ni subíndices Unicode (escribe CO2e en lugar de CO₂e)."
                    )
                    prompt_usuario = (
                        f"Datos consolidados del turno:\n"
                        f"- Total de emisiones de metano mitigadas: {iepm_mitigado:.2f} kg CO2e (Desvío del {pct_desvio:.1f}%)\n"
                        f"- Escudo fiscal recuperado por donaciones (Ley 1990 / Art. 257 E.T.): {escudo_fiscal:,.0f} COP\n"
                        f"- Ingresos recuperados por remate en barra (50%): {ingreso_recuperado:,.0f} COP\n"
                        f"- Pérdida económica por merma inservible: {perdida_merma:,.0f} COP\n"
                        f"- Lotes principales: {resumen_lotes}\n\n"
                        f"Redacta 2 o 3 párrafos de balance formal con conclusiones y recomendaciones operativas."
                    )

                    mensajes = [
                        {"role": "system", "content": prompt_sistema},
                        {"role": "user", "content": prompt_usuario}
                    ]
                    resultado = consultar_lm_studio(mensajes)

                    if resultado:
                        st.session_state.reporte_ia = limpiar_texto_pdf(resultado)
                    else:
                        st.error("⚠️ No se pudo conectar con LM Studio. Verifica que el servidor local esté activo en el puerto 1234.")

        # Botón de Descarga PDF
        if len(st.session_state.inventario) > 0:
            pdf_bytes = generar_pdf_informe(lote_procesado, kpis_dict, st.session_state.reporte_ia)
            with col_rep2:
                st.download_button(
                    label="📄 Descargar Informe de Turno en PDF",
                    data=pdf_bytes,
                    file_name=f"Informe_MethaneZero_{time.strftime('%Y%m%d_%H%M')}.pdf",
                    mime="application/pdf",
                    use_container_width=True
                )

        if st.session_state.reporte_ia:
            st.markdown("---")
            texto_seguro = st.session_state.reporte_ia.replace("$", r"\$")
            st.markdown(texto_seguro)

    with tab_chatbot:
        st.subheader("Asistente Virtual de Operación")
        st.caption("Consulta dudas conceptuales, operativas o regulatorias sobre la vitrina.")

        for msg in st.session_state.mensajes_chat:
            with st.chat_message(msg["role"]):
                st.markdown(msg["content"].replace("$", r"\$"))

        pregunta = st.chat_input("Escribe tu consulta aquí...")
        if pregunta:
            st.session_state.mensajes_chat.append({"role": "user", "content": pregunta})
            with st.chat_message("user"):
                st.markdown(pregunta.replace("$", r"\$"))

            with st.chat_message("assistant"):
                with st.spinner("Consultando con el asistente..."):
                    lineas_inventario = []
                    for p in lote_procesado:
                        lineas_inventario.append(
                            f"• Producto: {p['nombre']} | Categoría: {p['categoria']} | "
                            f"Horas útiles restantes: {p['horas_reales']} h | Canal asignado: {p['canal']} | "
                            f"IEPM: {p['iepm']} kg CO2e | Criticidad: {p['criticidad']}"
                        )
                    contexto_inventario = "\n".join(lineas_inventario) if lineas_inventario else "La vitrina está vacía actualmente."

                    system_prompt = (
                        "Eres MethaneZero-Bot, el asistente técnico de la cafetería universitaria.\n\n"
                        "COMPORTAMIENTO SEGÚN EL TIPO DE MENSAJE:\n"
                        "1. Si el usuario solo saluda (ej. 'hola', 'buenos días'): responde con un saludo cordial y breve, "
                        "ofreciendo ayuda sobre los productos en vitrina, indicadores del turno, el IEPM o la Ley 1990.\n"
                        "2. Si el usuario pregunta sobre el proyecto o la vitrina: responde con precisión técnica usando los datos disponibles.\n"
                        "3. Si el usuario menciona problemas personales, emociones, rupturas sentimentales o temas ajenos al sistema: "
                        "valida su estado en una frase corta sin ofrecer ayuda externa ni líneas telefónicas, y aclara que "
                        "solo estás habilitado para atender consultas técnicas sobre MethaneZero-AI.\n\n"
                        "BASE TEÓRICA DEL PROYECTO:\n"
                        "- IEPM (Índice de Emisión Potencial de Metano): Masa de gas de efecto invernadero (en kg CO2e) "
                        "que liberaría el alimento al descomponerse sin oxígeno en relleno sanitario: IEPM = Peso en kg * Factor Bromatológico * 28.\n"
                        "- Criticidad: Urgencia de salida por hora restante: Criticidad = IEPM / Horas Reales Restantes.\n"
                        "- Marco Legal (Ley 1990 de 2019 / Art. 257 E.T.): Alimentos con 6 horas o menos de vida útil "
                        "se enrutan a donación certificada, otorgando un descuento del 25% del valor donado en el impuesto de renta.\n\n"
                        "REGLAS DE FORMATO:\n"
                        "- Escribe en texto plano limpio en español, sin comandos de LaTeX.\n"
                        "- Usa exclusivamente los valores calculados por Python para los productos en vitrina.\n\n"
                        f"ESTADO DE LA VITRINA:\n{contexto_inventario}"
                    )

                    historial_para_api = [{"role": "system", "content": system_prompt}]
                    for m in st.session_state.mensajes_chat[-6:]:
                        historial_para_api.append({"role": m["role"], "content": m["content"]})

                    respuesta_bot = consultar_lm_studio(historial_para_api)

                    if respuesta_bot:
                        respuesta_bot = limpiar_texto_pdf(respuesta_bot)
                        respuesta_bot = respuesta_bot.replace(r"\times", "×")
                        respuesta_bot = respuesta_bot.replace(r"\text", "")
                        respuesta_bot = respuesta_bot.replace("\\[", "").replace("\\]", "")

                        st.markdown(respuesta_bot.replace("$", r"\$"))
                        st.session_state.mensajes_chat.append({"role": "assistant", "content": respuesta_bot})
                    else:
                        error_msg = "⚠️ LM Studio no está respondiendo. Verifica que el servidor local esté activo."
                        st.warning(error_msg)
                        st.session_state.mensajes_chat.append({"role": "assistant", "content": error_msg})