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

# Свързване с Google Sheets
try:
    client = gspread.service_account(filename="/etc/secrets/credentials.json")
    sheet = client.open("Raid_CvC_Ultimate_Strategy_Tracker")
    print("✅ БОТ-КУРИЕР: Успешна връзка с Google Sheets!")
except Exception as e:
    print(f"❌ Грешка при връзка с Sheets: {e}")

def get_worksheet_safely(sheet_obj, target_name):
    for ws in sheet_obj.worksheets():
        if ws.title.strip().lower() == target_name.strip().lower(): return ws
    return sheet_obj.worksheet(target_name)

# ========================================================
# ИНТЕРФЕЙС 1: ГЛАСУВАНЕ ЗА СТРАТЕГИЯ
# ========================================================
class StrategyVoteView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def register_vote(self, interaction: discord.Interaction, vote_value: str):
        await interaction.response.defer(ephemeral=True)
        ws = get_worksheet_safely(sheet, "Резултати_Бизнес")
        names = ws.col_values(1)
        user_name = str(interaction.user.name).strip()
        
        row_idx = -1
        for i, name in enumerate(names):
            if name.strip().lower() == user_name.lower():
                row_idx = i + 1
                break
        
        if row_idx == -1:
            ws.append_row([user_name, "0", "0", "0", "0", "0", vote_value])
        else:
            ws.update_cell(row_idx, 7, vote_value)
            
        ws_proc = get_worksheet_safely(sheet, "CvC_Процедура")
        total_yes = ws_proc.cell(1, 3).value # Прочита клетка C1 на живо
        
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
            ws = get_worksheet_safely(sheet, "Резултати_Бизнес")
            names = ws.col_values(1)
            
            row_idx = -1
            for i, name in enumerate(names):
                if name.strip().lower() == user_name.lower():
                    row_idx = i + 1
                    break
                    
            if row_idx == -1:
                ws.append_row([user_name, *vals, "Още няма избор"])
                row_idx = len(names) + 1
            else:
                for col, val in enumerate(vals, start=2):
                    ws.update_cell(row_idx, col, val)
            
            # Умно изчакване: проверява на всеки 2 секунди (общо до 12 сек.) дали AI е готов
            ai_advice = ""
            for _ in range(6):
                await asyncio.sleep(2.0)
                val = str(ws.cell(row_idx, 9).value or "").strip()
                if val and "Зареждане" not in val and "Loading" not in val:
                    ai_advice = val
                    break
            
            predicted_points = ws.cell(row_idx, 8).value
            
            if not ai_advice:
                ai_advice = "⏳ AI стратегът все още обработва данните. Провери след малко в таблицата!"
            
            msg = (
                f"📊 **ПРОГНОЗА ЗА CvC ВОЙНАТА**\n\n"
                f"🦾 Въз основа на твоите 5 ресурса, системата прогнозира: **{predicted_points} базови точки**.\n\n"
                f"🧠 **ПЕРСОНАЛЕН СЪВЕТ ОТ AI СТРАТЕГА:**\n"
                f"{ai_advice}"
            )
            await interaction.followup.send(msg, ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Грешка при обработка на данните: {e}", ephemeral=True)

class LaunchPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="📊 Стартирай 5-Ресурсен Калкулатор", style=discord.ButtonStyle.blurple, custom_id="btn_open_5inputs")
    async def open_modal(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(DynamicInputsModal())

# ========================================================
# ДИРЕКТНА ВРЪЗКА С GEMINI API + АРХИВ В GOOGLE SHEETS
# ========================================================
async def ask_gemini_direct(question, clan_context):
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        return "❌ Липсва GEMINI_API_KEY в настройките на сървъра (Render)."
    
    domain = ".".join(["generativelanguage", "googleapis", "com"])
    url = f"https://{domain}/v1beta/interactions"
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
    
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=payload) as resp:
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
    if message.author == bot.user: return

    # Куриер 1: Извикване на Процедурата и Гласуването (Публично)
    if message.content == "?преглед_стратегия":
        ws_proc = get_worksheet_safely(sheet, "CvC_Процедура")
        f1_text = ws_proc.cell(1, 1).value
        f2_text = ws_proc.cell(1, 2).value
        
        embed = discord.Embed(title="⚔️ СТРАТЕГИЧЕСКА КАРТА ЗА CvC ⚔️", color=discord.Color.gold())
        embed.add_field(name="📅 Сценарий и Времеви хоризонт", value=f1_text, inline=False)
        embed.add_field(name="💶 Действия и Бизнес ROI Анализ", value=f2_text, inline=False)
        
        await message.channel.send(embed=embed, view=StrategyVoteView())
        return

    # Куриер 2: Извикване на 5-Ресурсния калкулатор (Публично)
    if message.content == "?панел_прогнози":
        await message.channel.send(
            "🔮 **КОНТРОЛЕН ПАНЕЛ ЗА ПРОГНОЗИРАНЕ**\n"
            "Въведете наличностите си, за да изчислим стартовия ви потенциал и да получите AI анализ:",
            view=LaunchPanelView()
        )
        return

    # Куриер 3: Директен въпрос към AI Стратега + Четене от База_Знания + Лог
    if message.content.startswith("?питай_стратег"):
        question = message.content[len("?питай_стратег"):].strip()
        if not question:
            await message.channel.send("❓ Напиши въпроса си след командата. Пример: `?питай_стратег Колко точки дават 1000 магма тухлички при 2x?`")
            return
        
        async with message.channel.typing():
            try:
                ws_proc = get_worksheet_safely(sheet, "CvC_Процедура")
                f1 = ws_proc.cell(1, 1).value or ""
                f2 = ws_proc.cell(1, 2).value or ""
                custom_rules = ws_proc.cell(1, 4).value or ""
                
                # Четене на допълнителната информация от лист База_Знания
                kb_text = ""
                try:
                    ws_kb = get_worksheet_safely(sheet, "База_Знания")
                    kb_rows = ws_kb.get_all_values()
                    if len(kb_rows) > 1:
                        kb_lines = [f"- {' : '.join(r)}" for r in kb_rows[1:] if any(r)]
                        kb_text = "\n".join(kb_lines)
                except Exception:
                    kb_text = ""
                
                clan_context = (
                    f"Фаза 1: {f1}\n"
                    f"Фаза 2: {f2}\n"
                    f"Главни инструкции и забрани: {custom_rules}\n"
                    f"База знания на клана:\n{kb_text}"
                )
                
                answer = await ask_gemini_direct(question, clan_context)
                
                # Записваме въпроса и отговора в таб AI_История
                try:
                    ws_hist = get_worksheet_safely(sheet, "AI_История")
                    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    ws_hist.append_row([now_str, str(message.author.name), question, answer])
                except Exception as hist_err:
                    print(f"Грешка при запис в AI_История: {hist_err}")
                
                await message.reply(f"🧠 **AI Стратег:**\n{answer}")
            except Exception as e:
                await message.reply(f"❌ Възникна грешка при обработката: `{e}`")
        return

    await bot.process_commands(message)

# Бекграунд уеб сървър за Render
async def main():
    app = web.Application()
    app.router.add_get('/', lambda r: web.Response(text="Ботът работи!"))
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, '0.0.0.0', int(os.environ.get("PORT", 10000))).start()
    await bot.start(os.environ.get("DISCORD_TOKEN"))

if __name__ == "__main__":
    asyncio.run(main())
