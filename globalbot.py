import discord
from discord.ext import commands
import gspread
from aiohttp import web
import asyncio
import os
import aiohttp
from datetime import datetime

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="?", intents=intents)

# Глобална HTTP сесия и кеш за листовете
http_session: aiohttp.ClientSession = None
ws_cache = {}

try:
    client = gspread.service_account(filename="/etc/secrets/credentials.json")
    sheet = client.open("Raid_CvC_Ultimate_Strategy_Tracker")
    print("✅ БОТ-КУРИЕР: Успешна връзка с Google Sheets!")
except Exception as e:
    print(f"❌ Грешка при връзка с Sheets: {e}")

def get_worksheet_cached(target_name: str):
    key = target_name.strip().lower()
    if key in ws_cache:
        return ws_cache[key]
    
    for ws in sheet.worksheets():
        if ws.title.strip().lower() == key:
            ws_cache[key] = ws
            return ws
    ws = sheet.worksheet(target_name)
    ws_cache[key] = ws
    return ws

# ========================================================
# ПОМОЩНИ СИНХРОННИ ФУНКЦИИ (За изпълнение в отделна нишка)
# ========================================================
def sync_register_vote(user_name: str, vote_value: str) -> str:
    ws = get_worksheet_cached("Резултати_Бизнес")
    names = ws.col_values(1)
    
    row_idx = next((i + 1 for i, n in enumerate(names) if n.strip().lower() == user_name.lower()), -1)
    
    if row_idx == -1:
        ws.append_row([user_name, "0", "0", "0", "0", "0", vote_value])
    else:
        ws.update_cell(row_idx, 7, vote_value)
        
    ws_proc = get_worksheet_cached("CvC_Процедура")
    return ws_proc.acell("C1").value

def sync_save_resources(user_name: str, vals: list) -> int:
    ws = get_worksheet_cached("Резултати_Бизнес")
    names = ws.col_values(1)
    
    row_idx = next((i + 1 for i, n in enumerate(names) if n.strip().lower() == user_name.lower()), -1)
    
    if row_idx == -1:
        ws.append_row([user_name, *vals, "Още няма избор"])
        return len(names) + 1
    else:
        # 1 заявка за всички 5 колони (B до F) вместо 5 отделни!
        ws.update(f"B{row_idx}:F{row_idx}", [vals])
        return row_idx

def sync_check_ai_result(row_idx: int):
    ws = get_worksheet_cached("Резултати_Бизнес")
    # Четем H и I едновременно с 1 заявка!
    res = ws.get(f"H{row_idx}:I{row_idx}")
    if not res or not res[0]:
        return None, ""
    row_data = res[0]
    points = row_data[0] if len(row_data) > 0 else "0"
    advice = row_data[1].strip() if len(row_data) > 1 else ""
    return points, advice

def sync_get_strategy_texts():
    ws_proc = get_worksheet_cached("CvC_Процедура")
    row1 = ws_proc.row_values(1) # 1 заявка за целия ред!
    f1 = row1[0] if len(row1) > 0 else ""
    f2 = row1[1] if len(row1) > 1 else ""
    return f1, f2

def sync_get_clan_context() -> str:
    ws_proc = get_worksheet_cached("CvC_Процедура")
    row1 = ws_proc.row_values(1) # 1 заявка вместо 3
    f1 = row1[0] if len(row1) > 0 else ""
    f2 = row1[1] if len(row1) > 1 else ""
    custom_rules = row1[3] if len(row1) > 3 else ""
    
    kb_text = ""
    try:
        ws_kb = get_worksheet_cached("База_Знания")
        kb_rows = ws_kb.get_all_values()
        if len(kb_rows) > 1:
            kb_lines = [f"- {' : '.join(r)}" for r in kb_rows[1:] if any(r)]
            kb_text = "\n".join(kb_lines)
    except Exception:
        pass
        
    return (
        f"Фаза 1: {f1}\n"
        f"Фаза 2: {f2}\n"
        f"Главни инструкции и забрани: {custom_rules}\n"
        f"База знания на клана:\n{kb_text}"
    )

def sync_log_history(user_name: str, question: str, answer: str):
    try:
        ws_hist = get_worksheet_cached("AI_История")
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ws_hist.append_row([now_str, user_name, question, answer])
    except Exception as hist_err:
        print(f"Грешка при запис в AI_История: {hist_err}")

# ========================================================
# ИНТЕРФЕЙС 1: ГЛАСУВАНЕ ЗА СТРАТЕГИЯ
# ========================================================
class StrategyVoteView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def register_vote(self, interaction: discord.Interaction, vote_value: str):
        await interaction.response.defer(ephemeral=True)
        user_name = str(interaction.user.name).strip()
        
        # Неблокиращо извикване
        total_yes = await asyncio.to_thread(sync_register_vote, user_name, vote_value)
        await interaction.followup.send(f"📊 Вашият избор беше записан! Общо потвърдили до момента: **{total_yes} играчи**.", ephemeral=True)

    @discord.ui.button(label="🟢 Да, приемам стратегията", style=discord.ButtonStyle.green, custom_id="vote_yes")
    async def vote_yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.register_vote(interaction, "🟢 ДА")

    @discord.ui.button(label="🔴 Не, ще играя по мой начин", style=discord.ButtonStyle.danger, custom_id="vote_no")
    async def vote_no(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.register_vote(interaction, "🔴 НЕ")

# ========================================================
# ИНТЕРФЕЙС 2: 5-КРИТИЧНИ РЕСУРСА (MODAL) + AI СЪВЕТНИК
# ========================================================
class DynamicInputsModal(discord.ui.Modal, title="📊 5 Критични Ресурса за CvC"):
    silver = discord.ui.TextInput(label="Сребро (в милиони):", placeholder="Напр. 40", default="0")
    prepped = discord.ui.TextInput(label="Заготовки на +11 (брой):", placeholder="Напр. 36", default="0")
    cores = discord.ui.TextInput(label="Магма тухлички (брой):", placeholder="Напр. 1200", default="0")
    glyphs = discord.ui.TextInput(label="Глифове (брой):", placeholder="Напр. 50", default="0")
    energy = discord.ui.TextInput(label="Налична Енергия:", placeholder="Напр. 3000", default="0")

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        user_name = str(interaction.user.name).strip()
        try:
            vals = [int(self.silver.value), int(self.prepped.value), int(self.cores.value), int(self.glyphs.value), int(self.energy.value)]
            
            row_idx = await asyncio.to_thread(sync_save_resources, user_name, vals)
            
            ai_advice = ""
            predicted_points = "0"
            
            # Изчакваме на по-редки интервали (3 сек.), за да пестим API квота
            for _ in range(4):
                await asyncio.sleep(3.0)
                points, val = await asyncio.to_thread(sync_check_ai_result, row_idx)
                predicted_points = points
                if val and "Зареждане" not in val and "Loading" not in val:
                    ai_advice = val
                    break
            
            if not ai_advice:
                ai_advice = "⏳ AI стратегът все още обработва данните. Провери след малко в таблицата!"
            
            msg = (
                f"📊 **ПРОГНОЗА ЗА CvC ВОЙНАТА**\n\n"
                f"🦾 Въз основа на твоите 5 ресурса, системата прогнозира: **{predicted_points} базови точки**.\n\n"
                f"🧠 **ПЕРСОНАЛЕН СЪВЕТ ОТ AI СТРАТЕГА:**\n"
                f"{ai_advice}"
            )
            await interaction.followup.send(msg, ephemeral=True)
        except ValueError:
            await interaction.followup.send("❌ Моля, въвеждай само цели числа!", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Грешка при обработка на данните: {e}", ephemeral=True)

class LaunchPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="📊 Стартирай 5-Ресурсен Калкулатор", style=discord.ButtonStyle.blurple, custom_id="btn_open_5inputs")
    async def open_modal(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(DynamicInputsModal())

# ========================================================
# ДИРЕКТНА ВРЪЗКА С GEMINI API
# ========================================================
async def ask_gemini_direct(question: str, clan_context: str) -> str:
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        return "❌ Липсва GEMINI_API_KEY в настройките на сървъра (Render)."
    
    url = "https://googleapis.com"
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": api_key,
        "Api-Revision": "2026-05-20"
    }
    
    prompt = (
        f"Ти си топ стратег в Raid: Shadow Legends и помагаш на играчите в клана.\n"
        f"Правила, стратегия и база знания на клана от таблицата:\n{clan_context}\n\n"
        f"Въпрос от играча: {question}\n"
        f"Отговори ясно, конкретно и приятелски на български език (до 4-5 изречения)."
    )
    
    payload = {
        "model": "gemini-3.5-flash-lite",
        "input": prompt
    }
    
    # Използваме глобалната HTTP сесия
    async with http_session.post(url, headers=headers, json=payload) as resp:
        data = await resp.json()
        if "error" in data:
            return f"❌ Грешка от Gemini: {data['error'].get('message', 'Неизвестна грешка')}"
        
        steps = data.get("steps", [])
        for step in reversed(steps):
            if step.get("type") == "model_output":
                for item in step.get("content", []):
                    if item.get("type") == "text":
                        return item.get("text", "")
        return f"⏳ Неочакван отговор от модела: {str(data)[:150]}"

# ========================================================
# ОНЛАЙН СЪБИТИЯ И ТЕКСТОВИ КУРИЕРИ
# ========================================================
@bot.event
async def on_ready():
    bot.add_view(StrategyVoteView())
    bot.add_view(LaunchPanelView())
    print(f"🚀 БОТ-КУРИЕР Е ОНЛАЙН: {bot.user.name}")

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    if message.content == "?преглед_стратегия":
        f1_text, f2_text = await asyncio.to_thread(sync_get_strategy_texts)
        embed = discord.Embed(title="⚔️ СТРАТЕГИЧЕСКА КАРТА ЗА CvC ⚔️", color=discord.Color.gold())
        embed.add_field(name="📅 Сценарий и Времеви хоризонт", value=f1_text, inline=False)
        embed.add_field(name="💶 Действия и Бизнес ROI Анализ", value=f2_text, inline=False)
        await message.channel.send(embed=embed, view=StrategyVoteView())
        return

    if message.content == "?панел_прогнози":
        await message.channel.send(
            "🔮 КОНТРОЛЕН ПАНЕЛ ЗА ПРОГНОЗИРАНЕ\n"
            "Въведете наличностите си, за да изчислим стартовия ви потенциал и да получите AI анализ:",
            view=LaunchPanelView()
        )
        return

    if message.content.startswith("?питай_стратег"):
        question = message.content[len("?питай_стратег"):].strip()
        if not question:
            await message.channel.send("❓ Напиши въпроса си след командата. Пример: ?питай_стратег Колко точки дават 1000 магма тухлички при 2x?")
            return
        
        async with message.channel.typing():
            try:
                clan_context = await asyncio.to_thread(sync_get_clan_context)
                answer = await ask_gemini_direct(question, clan_context)
                # Записваме историята във фонов режим, без да бавим отговора към играча
                asyncio.create_task(asyncio.to_thread(sync_log_history, str(message.author.name), question, answer))
                await message.reply(f"🧠 AI Стратег:\n{answer}")
            except Exception as e:
                await message.reply(f"❌ Възникна грешка при обработката: {e}")
        return

    await bot.process_commands(message)

async def main():
    global http_session
    http_session = aiohttp.ClientSession()
    try:
        app = web.Application()
        app.router.add_get('/', lambda r: web.Response(text="Ботът работи!"))
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, '0.0.0.0', int(os.environ.get("PORT", 10000))).start()
        await bot.start(os.environ.get("DISCORD_TOKEN"))
    finally:
        await http_session.close()

if __name__ == "__main__":
    asyncio.run(main())
