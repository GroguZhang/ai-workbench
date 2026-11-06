"""
Semantic Chunking and Metadata Tagging Script for Musicology RAG
Author: Zheren Zhang
Description: Cleans raw historical text, splits it contextually by movements, 
and exports to JSONL format with metadata for Hybrid Search retrieval.
"""
import json
import re

def clean_and_chunk_corpus(input_file_path, output_file_path):
    """
    模拟将非结构化的音乐学文献清洗并转化为高质量 RAG 语料的过程
    """
    # 模拟原始语料数据
    raw_text = """
    第五部分 音乐本体深度分析
    5.6 第六乐章:《保卫黄河》
    进行曲风格:2/4拍,节奏鲜明有力...卡农(Canon)技法:这是复调音乐的一种形式...
    """
    
    print("[Info] Starting semantic chunking and metadata injection...")
    
    # 预处理：清洗 OCR 噪声、多余空格和空行
    clean_text = re.sub(r'\n\s*\n', '\n', raw_text.strip())
    
    # 核心创新：基于语义的上下文分块（Semantic Chunking）
    # 注入元数据标签，以便实现“向量相似度 + 元数据过滤”的混合检索
    chunks = [
        {
            "metadata": {
                "movement_id": 7,
                "movement_name": "保卫黄河",
                "category": "music_ontology",
                "core_technique": "Polyphonic Canon"
            },
            "content": "《保卫黄河》运用进行曲风格，2/4拍，节奏鲜明有力。核心采用了卡农（Canon）轮唱的复调技法，各声部先后进入模仿同一旋律，生动地象征了抗日游击队从四面八方包围敌人的壮大场面。"
        },
        {
             "metadata": {
                "movement_id": "general",
                "category": "historical_background",
                "instrumentation": "Gasoline Drum"
            },
            "content": "在延安首演时，由于缺乏低音弦乐器，鲁艺师生将汽油桶改造成共鸣箱，制作成土大提琴/低音胡，具有浑厚沉闷的共鸣感。"
        }
    ]
    
    # 导出为标准的 JSONL 格式（大模型知识库导入的标准格式）
    with open(output_file_path, 'w', encoding='utf-8') as f:
        for chunk in chunks:
            f.write(json.dumps(chunk, ensure_ascii=False) + '\n')
            
    print(f"[Success] Exported {len(chunks)} semantically structured chunks.")

if __name__ == "__main__":
    # clean_and_chunk_corpus("raw_corpus.txt", "kb_chunk_sample.jsonl")
    pass
