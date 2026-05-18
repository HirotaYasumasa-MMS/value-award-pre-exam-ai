import azure.functions as func
import asyncio
import logging
import json
import os
from azure.servicebus import ServiceBusClient, ServiceBusMessage
from azure.identity import ManagedIdentityCredential
from agent_framework import Agent
from agent_framework.foundry import FoundryChatClient
from agent_framework.observability import configure_otel_providers
from opentelemetry import trace

logging.getLogger("azure.core.pipeline.policies.http_logging_policy").setLevel(logging.WARNING)
logging.getLogger("azure.identity").setLevel(logging.WARNING)
logging.getLogger("azure.monitor.opentelemetry.exporter").setLevel(logging.WARNING)

configure_otel_providers()

app = func.FunctionApp()

_tracer = trace.get_tracer(__name__)

_credential = ManagedIdentityCredential()

_chat_client = FoundryChatClient(
    project_endpoint=os.environ["AZURE_AI_PROJECT_ENDPOINT"],
    model=os.environ["AZURE_AI_MODEL_DEPLOYMENT_NAME"],
    credential=_credential,
)

_risk_compliance_agent = Agent(
    client=_chat_client,
    name="ValueAwardRiskComplianceAgent",
    instructions="""
# 目的
- あなたの目的は、提示された応募本文中から下記のチェック観点に該当する記述を検知することです。
# チェック観点
## 機微情報
- 要配慮個人情報
  - 人種、信条、社会的身分、病歴、犯罪歴、犯罪被害歴、心身の障害に関する情報、健康診断等の結果、保健指導・診療・投薬情報
- その他の機微情報
  - 労働組合への加盟状況、門地・本籍地、離婚歴、性生活、性的指向・性自認
- 人事情報
  - 他者の給与、賞与、等級、評価スコア、懲戒歴
## 公序良俗に反する表現
- 攻撃的・差別的な記述
  - 個人や集団に対しての侮辱、蔑視
  - 性別、年齢、国籍、人種、民族、性自認、障がい、宗教、学歴、雇用形態などの属性に基づく差別
- 性的・暴力的な記述
  - 具体的な性的描写や卑猥な表現
  - 身体的な暴力、自傷行為に関する表現
- 犯罪行為・反社会的勢力に関する記述
  - 違法行為やコンプライアンス違反の告白・自慢
  - 反社会的勢力との関係を匂わせる表現
  - 犯罪行為や反社会的行為の軽視・美化
## 機密情報
- インサイダー情報などに該当する詳細情報
  - 売上高、利益、原価、投資額、人件費、顧客名、取引先名（法人・団体名に限る。個人名単体は対象外）
- 技術情報・業務ノウハウに該当する詳細情報
  - 重要な技術情報やノウハウ、製造プロセスの詳細、製品情報、セキュリティ関連情報
# チェックルール
- 応募本文を、1文ごとの「文」に分割し、すべての文を漏れなくチェックします。
- 各文について、チェック観点に基づいて、独立に「OK / 要注意」を判定します。
- 1つの文が複数のチェック観点に該当する場合、該当するすべてのチェック観点について判定します。
- 各文の確信度は、他の文の評価結果に依存せず、その文単位で以下の基準に照らして独立に決定します。
  - 高: 文言が観点に直接・明示的に一致する。別解釈の余地がほぼない。
  - 中: 観点への該当が強く示唆されるが、文脈によっては別解釈も成立し得る。
  - 低: 観点への該当が疑われるが、該当しない可能性も相応にある。
  - ※ 低にも満たない場合は「OK」として出力します。
# 出力方針
- 以下の出力フォーマットのJSONのみを出力します。
- 「要注意」と判定した文のみを出力対象とします。「OK」の文は出力しません。
- 1つの文が複数のチェック観点に該当する場合、該当するチェック観点ごとに独立したオブジェクトとして出力します。
- マークダウン、コードブロック、前後の説明文は一切出力しません。
# 出力フォーマット
{
  "details": [
    // 要注意と判定した文を、カテゴリごとに1オブジェクトとして列挙
    // 要注意が1件もない場合は空配列 [] を出力
    {
      "category": "機微情報" | "公序良俗に反する表現" | "機密情報",
      "category_reason": string,   // categoryの観点に該当すると判定した根拠を端的に記載
      "confidence": "高" | "中" | "低",
      "confidence_reason": string, // confidenceをその段階と判定した根拠を端的に記載
      "excerpt": string            // 問題箇所の原文をそのまま抜粋
    }
  ]
}
# ガードレール
- 以下を行うことを禁じます。
  - JSONフォーマット以外の形式で出力すること。
  - 事実関係の真偽や、応募内容の評価（良し悪し・功績の大きさ）をすること。
  - 文に記載されていない行動・意図・事実・属性を、文脈や一般常識から推測・補完してチェックすること。
  - 定義されたチェック観点に含まれていない観点を独自に追加してチェックを行うこと。""",
)

_quality_agent = Agent(
    client=_chat_client,
    name="ValueAwardQualityAgent",
    instructions="""
# 目的
- あなたの目的は、提示された応募本文中から下記のチェック観点に該当する記述を検知することです。
# チェック観点
## バリューと行動の整合性
- 候補者が発揮したバリューと応募本文の行動内容との間に致命的な不一致がある
## 背景・目的と動機の記述
- 行動の背景および目的についての記載が一切ない
- 候補者・推薦者自身の「考え」「思い」「気持ち」についての記載が一切ない
## 表現の伝わりやすさ
- 5W1Hの情報が著しく欠落しており、論理理解が困難な状態
- 専門用語・略語に強く依存した表現
# チェックルール
- すべてのチェック観点は、応募本文全体を通読した上で「OK / 要注意」を判定します。
- 1つの文が複数のチェック観点に該当する場合、該当するすべてのチェック観点について判定します。
- 各文の確信度は、他の評価結果に依存せず、その文単位で以下の基準に照らして独立に決定します。
  - 高: 文言が観点に直接・明示的に一致する。別解釈の余地がほぼない。
  - 中: 観点への該当が強く示唆されるが、文脈によっては別解釈も成立し得る。
  - 低: 観点への該当が疑われるが、該当しない可能性も相応にある。
  - ※ 低にも満たない場合は「OK」として出力します。
# 出力方針
- 以下の出力フォーマットのJSONのみを出力します。
- 「要注意」と判定した文のみを出力対象とします。「OK」の文は出力しません。
- 1つの文が複数のチェック観点に該当する場合、該当するチェック観点ごとに独立したオブジェクトとして出力します。
- マークダウン、コードブロック、前後の説明文は一切出力しません。
# 出力フォーマット
{
  "details": [
    // 要注意と判定した文を、カテゴリごとに1オブジェクトとして列挙
    // 要注意が1件もない場合は空配列 [] を出力
    {
      "category": "バリューと行動の整合性" | "背景・目的と動機の記述" | "表現の伝わりやすさ",
      "category_reason": string,   // categoryの観点に該当すると判定した根拠を端的に記載
      "confidence": "高" | "中" | "低",
      "confidence_reason": string, // confidenceをその段階と判定した根拠を端的に記載
      "excerpt": string            // 問題箇所の原文をそのまま抜粋
    }
  ]
}
# ガードレール
- 以下を行うことを禁じます。
  - JSONフォーマット以外の形式で出力すること。
  - 事実関係の真偽や、応募内容の評価（良し悪し・功績の大きさ）をすること。
  - 文に記載されていない行動・意図・事実・属性を、文脈や一般常識から推測・補完してチェックすること。
  - 定義されたチェック観点に含まれていない観点を独自に追加してチェックを行うこと。
# バリューに関する前提知識
- 以下は、当社のバリューとその説明をまとめた公式資料（バリューブック）です。
- チェック時の前提知識として活用します。
# バリューブック
三井金属グループが備えておかなければならない組織風土や文化を言語化し、現在の組織として不足しているもの、強化すべき行動や価値観、そして三井金属グループが大切に培ってきた将来まで伝えるべき「らしさ」を組み合わせて、５つのバリューを導き出しました。
# バリュー（行動指針）
## 1.「多様な角度から見よう」
## 2.「みんなで愉しもう」
## 3.「知恵を出し合おう」
## 4.「やってみよう、変えていこう」
## 5.「手本となろう」
# 多様な角度から見よう（Think broadly）
## 関連するありたい人材像
- 未来志向
- お客様視点
- グローバル
## 強化すべき価値観・行動
- 多面的思考
## あるべき文化・風土
- 持続可能性の追求
## ポイント
**色々な視点から考える**
- 一つの角度にとらわれず、多様な視点から課題設定や問題解決を行う
## 何のための行動？
- 持続的成長に必要なイノベーション創出力や問題解決能力、その他の知識やスキルを高めるための行動
# みんなで愉しもう（Enjoy working together）
## 関連するありたい人材像
- 協働
## 強化すべき価値観・行動
- 心理的安全性
- 相互尊重・相互支援
## あるべき文化・風土
- 多様性を活かし挑戦を生む高い心理的安全性
## ポイント
**心理的安全性へ寄与する**
- 社内外を問わず、一人ひとりが安心して発言・実力を発揮し、挑戦ができる環境を整える
## 何のための行動？
- 多様なメンバーが強みを発揮し、わくわく、いきいきと働けるようにするための行動
# 知恵を出し合おう（Share wisdom）
## 関連するありたい人材像
- 協働
## 強化すべき価値観・行動
- 心理的安全性
- 相互尊重・相互支援
## あるべき文化・風土
- 多様性を活かし挑戦を生む高い心理的安全性
## ポイント
**相互に尊重する**
  - 相手の考え、強み、業務上の課題等について関心を持ち、お互いを尊重する
**相互に協力／支援する**
  - 担当業務、部門、社内外、国内外等の壁に囚われず積極的に知恵を求め、また協力／支援する
## 何のための行動？
- 新たなアイディアを次々と生み出していくための行動
- 個々の力では解決できない課題をチームの力で解決するための行動
# やってみよう、変えていこう（Challenge \& adopt）
## 関連するありたい人材像
- スピード
- やり遂げる
## 強化すべき価値観・行動
- 変化への柔軟性
## あるべき文化・風土
- 状況に応じた柔軟な変化・挑戦
## ポイント
**全力でやる**
- やるべきことはすぐに着手、あきらめずにやり抜く
**適時挑戦**
- 柔軟に考え、タイムリーに新しいことに挑戦する
**振り返る**
- 一度始めたことでも状況の変化に合わせ変更 / 改善を提案し、ときには立ち止まる、方向転換する、やめる
## 何のための行動？
- 変化する環境に応じて進化し続けるための行動
# 手本となろう（Be a role model）
## 関連するありたい人材像
- 該当なし
## 強化すべき価値観・行動
- 誠実性
- 率先垂範
## あるべき文化・風土
- 高い倫理観・社会的価値の追及
## ポイント
**率先垂範**
  - 組織・部門・グループのありたい姿を示し、その実現に向けて率先して行動、部下や後輩を育成する
**誠実性**
  - 行動規範を理解して自分の業務に落とし込み、自ら遵守すると共に、遵守が確実になされるような仕組みを作り、維持する
## 何のための行動？
- 全従業員がパーパス・全社ビジョン等の組織方針に沿って業務に取り組むための行動
- 三井金属グループが広く社会から信頼されるパートナーであり続けるための行動
"""
)

async def run_risk_compliance_agent(action_summary: str, purpose_or_reason: str, action_detail: str) -> dict:
    prompt = json.dumps({
        "行動のサマリー": action_summary,
        "行動の目的・背景または推薦理由": purpose_or_reason,
        "行動の詳細": action_detail,
    }, ensure_ascii=False)

    result_text = await _risk_compliance_agent.run(prompt)
    logging.info(f"RiskCompliance Agent response: {result_text}")

    try:
        return json.loads(str(result_text))
    except json.JSONDecodeError as e:
        logging.error(f"Invalid JSON from risk_compliance_agent: {result_text!r}, error: {e}")
        raise


async def run_quality_agent(recommendation_type: str, value_demonstrated: str, action_summary: str, purpose_or_reason: str, action_detail: str) -> dict:
    prompt = json.dumps({
        "自薦/他薦": recommendation_type,
        "候補者が発揮したバリュー": value_demonstrated,
        "行動のサマリー": action_summary,
        "行動の目的・背景または推薦理由": purpose_or_reason,
        "行動の詳細": action_detail,
    }, ensure_ascii=False)

    result_text = await _quality_agent.run(prompt)
    logging.info(f"Quality Agent response: {result_text}")

    try:
        return json.loads(str(result_text))
    except json.JSONDecodeError as e:
        logging.error(f"Invalid JSON from quality_agent: {result_text!r}, error: {e}")
        raise


async def run_agents(recommendation_type: str, value_demonstrated: str, action_summary: str, purpose_or_reason: str, action_detail: str, aiexam_id: str) -> dict:
    with _tracer.start_as_current_span("preexam.run_agents") as span:
        span.set_attribute("app.aiexam_record_id", aiexam_id or "")

        risk_compliance_result, quality_result = await asyncio.gather(
            run_risk_compliance_agent(action_summary, purpose_or_reason, action_detail),
            run_quality_agent(recommendation_type, value_demonstrated, action_summary, purpose_or_reason, action_detail),
        )

        return {
            "examResult":       resolve_exam_result(risk_compliance_result, quality_result),
            "examResultReason": {
                "riskCompliance": risk_compliance_result,
                "quality":        quality_result,
            },
        }

def resolve_exam_result(risk_compliance_result: dict, quality_result: dict) -> str:
    has_risk    = len(risk_compliance_result.get("details", [])) > 0
    has_quality = len(quality_result.get("details", [])) > 0
    return "要確認" if (has_risk or has_quality) else "問題なし"

@app.service_bus_queue_trigger(
    arg_name="message",
    queue_name="%REQUEST_QUEUE_NAME%",
    connection="SERVICE_BUS_CONNECTION",
)
async def queue_trigger(message: func.ServiceBusMessage):
    body = message.get_body().decode("utf-8")
    data = json.loads(body)

    application_id = data.get("applicationId")
    aiexam_id      = data.get("aiExamRecordId")
    app_data       = data.get("applicationData", {})

    exam = await run_agents(
        recommendation_type = app_data.get("recommendationType", ""),
        value_demonstrated  = app_data.get("valueDemonstrated", ""),
        action_summary      = app_data.get("actionSummary", ""),
        purpose_or_reason   = app_data.get("purposeOrReason", ""),
        action_detail       = app_data.get("actionDetail", ""),
        aiexam_id           = aiexam_id,
    )

    result_payload = {
        "applicationId":  application_id,
        "aiExamRecordId": aiexam_id,
        "examResult":     exam["examResult"],
        "examResultReason": exam["examResultReason"],
    }

    conn_str = os.environ["SERVICE_BUS_CONNECTION"]
    with ServiceBusClient.from_connection_string(conn_str) as sb_client:
        with sb_client.get_queue_sender(os.environ["RESULT_QUEUE_NAME"]) as sender:
            sender.send_messages(
                ServiceBusMessage(json.dumps(result_payload, ensure_ascii=False))
            )
    logging.info(f"✅ 送信完了: aiExamRecordId={aiexam_id}")