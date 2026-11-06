"""
Asymmetric Information Delivery Workflow
Author: Zheren Zhang
Description: Generates multimodal rich cards for the frontend UI while simultaneously 
passing hidden pedagogical analysis (Ground Truth) to the LLM backend.
"""
import json

async def main(args):
    # ============================================================
    # 1. 核心数据区：各乐章配图与多模态配置
    # ============================================================
    IMAGES = {
        "1": "https://pic1.imgdb.cn/item/69a8e76ae2b0164ce37ae3c1.png",
        "2": "https://pic1.imgdb.cn/item/69a8e769e2b0164ce37ae3c0.png",
        "3": "https://pic1.imgdb.cn/item/69a8e766e2b0164ce37ae3bf.png",
        "4": "https://pic1.imgdb.cn/item/69a8e765e2b0164ce37ae3bd.png",
        "5": "https://pic1.imgdb.cn/item/69a8e765e2b0164ce37ae3be.png",
        "6": "https://pic1.imgdb.cn/item/69a8e763e2b0164ce37ae3bc.png",
        "7": "https://pic1.imgdb.cn/item/69a8e824e2b0164ce37ae3f4.png",
        "8": "https://pic1.imgdb.cn/item/69a8e824e2b0164ce37ae3f5.png"
    }

    MOVEMENTS_DATA = {
        "1": {
            "title": "🌊 第一乐章：黄河船夫曲",
            "card_text": "【请点击下方按钮聆听】\n这是全曲的序幕，请闭上眼，想象你正站在波涛汹涌的黄河岸边...",
            "hidden_analysis": "核心赏析点：运用了‘劳动号子’的音调与滚奏手法，描绘船夫在惊涛骇浪中搏斗的情景。",
            "video": "https://www.bilibili.com/video/BV1Hj421R74p",
            "btn_text": "👉 点击跳转B站聆听",
            "question": "听完这段，你听到了什么样的节奏？这让你想到了什么人在干什么？"
        },
        "2": {
            "title": "🏔️ 第二乐章：黄河颂",
            "card_text": "【请点击下方按钮聆听】\n男中音独唱开始了。注意听他的呼吸和语调，像不像在朗诵一首诗？",
            "hidden_analysis": "核心赏析点：气势宏伟的颂歌，以宽广的节奏和朗诵般的歌词，赞美黄河源远流长。",
            "video": "https://www.bilibili.com/video/BV1dm411R7FE",
            "btn_text": "👉 点击跳转B站聆听",
            "question": "这首歌像不像一个巨人在对母亲倾诉？你听出歌声中的那份自豪了吗？"
        },
        "3": {
            "title": "💧 第三乐章：黄河之水天上来",
            "card_text": "【请点击下方按钮聆听】\n这不仅是音乐，更是一段配乐诗朗诵。注意背景里的那个乐器声音。",
            "hidden_analysis": "核心赏析点：配乐诗朗诵。琵琶的轮指模拟流水的背景，诗人讲述民族苦难。",
            "video": "https://www.bilibili.com/video/BV19i421X7L5",
            "btn_text": "👉 点击跳转B站聆听",
            "question": "你能听出背景里一直响着的是什么乐器吗？它模拟了什么声音？"
        },
        "4": {
            "title": "🌾 第四乐章：黄水谣",
            "card_text": "【请点击下方按钮聆听】\n这是一首非常动人的女声合唱。请注意听乐曲中间情绪的变化。",
            "hidden_analysis": "核心赏析点：前半段描写平静安宁的田园生活，后半段控诉日寇入侵后的悲惨遭遇。",
            "video": "https://www.bilibili.com/video/BV1AF4m1F7WE",
            "btn_text": "👉 点击跳转B站聆听",
            "question": "你发现了吗？前半段和后半段的旋律情绪有什么不一样？为什么会有这种变化？"
        },
        "5": {
            "title": "🗣️ 第五乐章：河边对口曲",
            "card_text": "【请点击下方按钮聆听】\n这是两个老乡在聊天。听听看，他们聊得开心吗？",
            "hidden_analysis": "核心赏析点：模仿陕北民歌‘信天游’风格，用乐观幽默的方式表现抗战决心。",
            "video": "https://www.bilibili.com/video/BV1QK421Y7wy",
            "btn_text": "👉 点击跳转B站聆听",
            "question": "这种像聊天一样的唱法，是不是很有趣？你觉得他们对战争的态度是悲观的还是乐观的？"
        },
        "6": {
            "title": "🥀 第六乐章：黄河怨",
            "card_text": "【请点击下方按钮聆听】\n这是全曲最悲伤的一章。请准备好纸巾，仔细听这位妇女的哭诉。",
            "hidden_analysis": "核心赏析点：女高音独唱，如泣如诉，控诉战争给妇女儿童带来的深重灾难。",
            "video": "https://www.bilibili.com/video/BV1wx4y1S75o",
            "btn_text": "👉 点击跳转B站聆听",
            "question": "听到这里，你心里是什么感觉？这种如泣如诉的歌声，让你看到了什么画面？"
        },
        "7": {
            "title": "⚔️ 第七乐章：保卫黄河",
            "card_text": "【请点击下方按钮聆听】\n大家最熟悉的一首来了！注意听那种‘你追我赶’的演唱方式。",
            "hidden_analysis": "核心赏析点：轮唱（Canon）形式。描绘游击健儿从四面八方包围敌人的宏大场面。",
            "video": "https://www.bilibili.com/video/BV1pC41187vC",
            "btn_text": "👉 点击跳转B站聆听",
            "question": "这种一声接一声、此起彼伏的唱法叫‘轮唱’。你觉得它像不像战场上的什么战术？"
        },
        "8": {
            "title": "🦁 第八乐章：怒吼吧！黄河",
            "card_text": "【请点击下方按钮聆听】\n最终章！请感受那排山倒海的力量。",
            "hidden_analysis": "核心赏析点：全曲高潮。象征中华民族觉醒，发出抗战的最后怒吼。",
            "video": "https://www.bilibili.com/video/BV14J4m177SS",
            "btn_text": "👉 点击跳转B站聆听",
            "question": "最后的那个长音，是不是热血沸腾？这代表了我们民族什么样的决心？"
        }
    }

    # ============================================================
    # 2. 逻辑处理与模糊匹配（鲁棒性设计）
    # ============================================================
    try:
        params = args.params
    except:
        params = args

    user_query = str(params.get('user_query', '7'))

    # 模糊匹配逻辑：确保即便 LLM 传参微调也能精准定位
    target_id = "7" 
    if "1" in user_query or "一" in user_query or "船夫" in user_query: target_id = "1"
    elif "2" in user_query or "二" in user_query or "颂" in user_query: target_id = "2"
    elif "3" in user_query or "三" in user_query or "天上" in user_query: target_id = "3"
    elif "4" in user_query or "四" in user_query or "水谣" in user_query: target_id = "4"
    elif "5" in user_query or "五" in user_query or "对口" in user_query: target_id = "5"
    elif "6" in user_query or "六" in user_query or "怨" in user_query: target_id = "6"
    elif "7" in user_query or "七" in user_query or "保卫" in user_query: target_id = "7"
    elif "8" in user_query or "八" in user_query or "怒吼" in user_query: target_id = "8"
    
    data = MOVEMENTS_DATA.get(target_id, MOVEMENTS_DATA["7"])
    current_image = IMAGES.get(target_id, IMAGES["7"])
    
    # ============================================================
    # 3. 构造返回包（前后台数据解耦）
    # ============================================================
    return {
        "res": {
            # 渲染给用户的 UI 卡片数据
            "type": "rich_card",
            "title": data['title'],
            "content": data['card_text'], 
            "image_url": current_image,
            "video_url": data['video'],
            "button_text": data['btn_text'],
            
            # 传给 AI 的隐性教学指令
            "ai_teaching_guide": {
                "question_to_ask": data['question'],
                "knowledge_point": data['hidden_analysis']
            }
        }
    }
