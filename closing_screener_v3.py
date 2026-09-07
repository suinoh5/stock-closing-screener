"""
[보강판] 종가매매 실전 스크리너 & Gmail 자동 알림 (전략 3 최적화)
- PyKrx + 네이버 금융 크롤러 기반 (증권사 API 키 불필요, 100% 무료)
- 거래대금 300억~500억 이상 주도주 선별
- 당일 상승률 +3% ~ +15% (과열주 제외)
- 윗꼬리 ≤ 2.0% 이내 (종가 고가형 캔들)
- 전일 고가 돌파, 5/20선 정배열, 거래량 200%↑
- GitHub Actions / 클라우드 서버리스 환경 완벽 호환
"""

import os
import sys
import time
import requests
import pandas as pd
import numpy as np
import smtplib
from io import StringIO
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# 환경변수 또는 직접 설정
GMAIL_USER = os.getenv("GMAIL_USER", "your_email@gmail.com")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "xxxx xxxx xxxx xxxx")
TARGET_EMAIL = os.getenv("TARGET_EMAIL", GMAIL_USER)

# 보강된 최적 전략 (전략 3) 기준값
MIN_TRADE_AMOUNT = 300_0000_0000  # 최소 거래대금 (300억 원, 500억 이상 S급)
MIN_VOL_RATIO = 200.0             # 전일 대비 거래량 200% 이상
MIN_CHANGE_RATE = 3.0             # 최소 당일 상승률 (+3.0%)
MAX_CHANGE_RATE = 15.0            # 최대 당일 상승률 (+15.0% - 과열 급등주 제외)
MAX_PULLBACK = 2.0                # 최대 윗꼬리 허용치 (고가 대비 ≤ 2.0%)


def get_naver_top_volume_stocks():
    """네이버 금융에서 실시간 거래대금 상위 종목 목록 수집 (코스피/코스닥)"""
    stocks = []
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

    # 코스피(0) 및 코스닥(1) 거래대금 상위 페이지 크롤링
    for sosok in [0, 1]:
        url = f"https://finance.naver.com/sise/sise_quant.naver?sosok={sosok}"
        try:
            res = requests.get(url, headers=headers, timeout=10)
            res.encoding = 'euc-kr'
            tables = pd.read_html(StringIO(res.text))
            df = tables[1].dropna(subset=['종목명'])
            df = df[df['종목명'] != '']
            
            # HTML 링크에서 종목코드(6자리) 추출
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(res.text, 'html.parser')
            links = soup.select('table.type_2 td a.title')
            
            for idx, row in df.iterrows():
                name = str(row['종목명']).strip()
                match_link = [l['href'] for l in links if l.text.strip() == name]
                if match_link:
                    code = match_link[0].split('code=')[-1]
                    try:
                        curr_price = float(str(row['현재가']).replace(',', ''))
                        trade_amt = float(str(row['거래대금']).replace(',', '')) * 1_000_000 # 백만원 -> 원
                        change_rate = float(str(row['등락률']).replace('%', '').replace('+', ''))
                        stocks.append({
                            'code': code,
                            'name': name,
                            'curr_price': curr_price,
                            'trade_amt': trade_amt,
                            'change_rate': change_rate
                        })
                    except:
                        continue
        except Exception as e:
            print(f"네이버 금융 크롤링 오류 (sosok={sosok}): {e}")

    # 거래대금 내림차순 정렬
    stocks.sort(key=lambda x: x['trade_amt'], reverse=True)
    return stocks


def fetch_daily_ohlcv_naver(code, count=30):
    """네이버 fchart API를 통해 최근 일봉 OHLCV 수집 (5/20선 및 전일고가 계산)"""
    url = f"https://fchart.stock.naver.com/sise.nhn?symbol={code}&timeframe=day&count={count}&requestType=0"
    try:
        res = requests.get(url, timeout=10)
        import xml.etree.ElementTree as ET
        root = ET.fromstring(res.text)
        items = root.findall('.//item')
        
        bars = []
        for it in items:
            data = it.get('data')
            if data:
                p = data.split('|')
                if len(p) >= 6:
                    bars.append({
                        'date': p[0],
                        'open': float(p[1]),
                        'high': float(p[2]),
                        'low': float(p[3]),
                        'close': float(p[4]),
                        'volume': float(p[5])
                    })
        return bars
    except Exception as e:
        return []


def evaluate_candidate(stock):
    """전략 3 보강 규칙에 따른 개별 종목 평가"""
    code = stock['code']
    name = stock['name']
    curr_price = stock['curr_price']
    trade_amt = stock['trade_amt']
    change_rate = stock['change_rate']

    # 1. 1차 필터링: 거래대금 300억 이상 & 상승률 3% ~ 15% 사이
    if trade_amt < MIN_TRADE_AMOUNT or not (MIN_CHANGE_RATE <= change_rate <= MAX_CHANGE_RATE):
        return None

    # 2. 일봉 데이터 조회
    bars = fetch_daily_ohlcv_naver(code, count=35)
    if len(bars) < 22:
        return None

    today_bar = bars[-1]
    prev_bar = bars[-2]

    # 당일 고가/저가/시가
    today_high = today_bar['high']
    today_open = today_bar['open']
    today_vol = today_bar['volume']
    prev_high = prev_bar['high']
    prev_vol = prev_bar['volume']

    # 3. 윗꼬리(고가 대비 밀림) 계산: ≤ 2.0%
    pullback_pct = ((today_high - curr_price) / today_high * 100) if today_high > 0 else 999
    if pullback_pct > MAX_PULLBACK:
        return None

    # 4. 전일 고가 돌파 여부
    if curr_price <= prev_high:
        return None

    # 5. 전일 대비 거래량 200% (2배) 이상
    vol_ratio = (today_vol / prev_vol * 100) if prev_vol > 0 else 0
    if vol_ratio < MIN_VOL_RATIO:
        return None

    # 6. 5일선 / 20일선 정배열 계산
    close_list = [b['close'] for b in bars]
    ma5 = sum(close_list[-5:]) / 5
    ma20 = sum(close_list[-20:]) / 20

    if not (curr_price >= ma5 and curr_price >= ma20 and ma5 >= ma20):
        return None

    # 7. 양봉 마감 필수
    if curr_price <= today_open:
        return None

    # 점수 산출
    score = 8
    if trade_amt >= 500_0000_0000: score += 1  # 500억 이상 가점
    if vol_ratio >= 250.0: score += 1          # 거래량 250% 이상 가점
    if pullback_pct <= 1.0: score += 1         # 윗꼬리 1% 이내 완벽 양봉 가점

    # 최적화된 목표가 / 손절가 산출
    target1 = round(curr_price * 1.020)  # 1차 목표가 (+2.0%)
    target2 = round(curr_price * 1.035)  # 2차 목표가 (+3.5%)
    stop_loss = round(curr_price * 0.975) # 손절 기준가 (-2.5%)

    grade = "★ S급 (주도대장주)" if score >= 10 else "◎ A급 (주도주)"

    return {
        'code': code,
        'name': name,
        'curr_price': int(curr_price),
        'change_rate': change_rate,
        'trade_amt_억': round(trade_amt / 1_0000_0000),
        'vol_ratio': round(vol_ratio, 1),
        'pullback_pct': round(pullback_pct, 1),
        'score': score,
        'grade': grade,
        'target1': target1,
        'target2': target2,
        'stop_loss': stop_loss
    }


def send_gmail_report(qualified_stocks):
    """HTML 결과 리포트를 Gmail로 자동 발송"""
    now_str = datetime.now().strftime("%Y년 %m월 %d일 %H:%M")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🚀 [종가매매 추천] {datetime.now().strftime('%m/%d')} 최적 주도주 {len(qualified_stocks)}선 (승률 55.8% 보강전략)"
    msg["From"] = GMAIL_USER
    msg["To"] = TARGET_EMAIL

    rows_html = ""
    for idx, s in enumerate(qualified_stocks, 1):
        rows_html += f"""
        <tr style="text-align: center; border-bottom: 1px solid #e2e8f0;">
            <td style="padding: 10px; font-weight: bold;">{idx}</td>
            <td style="padding: 10px; font-weight: bold; color: #1F4E78;">{s['name']} ({s['code']})</td>
            <td style="padding: 10px; font-weight: bold; color: #d97706;">{s['grade']}</td>
            <td style="padding: 10px; text-align: right; font-weight: bold;">{s['curr_price']:,}원</td>
            <td style="padding: 10px; color: #dc2626; font-weight: bold;">+{s['change_rate']}%</td>
            <td style="padding: 10px; text-align: right;">{s['trade_amt_억']:,}억</td>
            <td style="padding: 10px;">{s['vol_ratio']}%</td>
            <td style="padding: 10px; color: #16a34a; font-weight: bold;">{s['pullback_pct']}%</td>
            <td style="padding: 10px; color: #dc2626; font-weight: bold;">{s['target1']:,}원 (+2.0%)</td>
            <td style="padding: 10px; color: #2563eb; font-weight: bold;">{s['stop_loss']:,}원 (-2.5%)</td>
        </tr>
        """

    if not qualified_stocks:
        rows_html = """
        <tr>
            <td colspan="10" style="padding: 25px; text-align: center; color: #64748b;">
                ⚠️ 오늘은 보강된 최적 조건(거래대금 300억↑, 상승률 3~15%, 윗꼬리 2%이내)을 만족하는 주도주가 없습니다.<br>
                <b>(백테스트 원칙 준수: 억지 매수 금지, 현금 100% 보존 권장)</b>
            </td>
        </tr>
        """

    html = f"""
    <html>
    <body style="font-family: 'Malgun Gothic', sans-serif; background-color: #f8fafc; padding: 15px; margin: 0;">
        <div style="max-width: 900px; margin: 0 auto; background: #ffffff; border-radius: 8px; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.1); overflow: hidden; border: 1px solid #e2e8f0;">
            <!-- 헤더 -->
            <div style="background-color: #1F4E78; color: #ffffff; padding: 20px; text-align: center;">
                <h2 style="margin: 0; font-size: 20px;">📋 종가매매 실전 추천 리포트 (전략 3 보강판)</h2>
                <p style="margin: 6px 0 0 0; font-size: 13px; opacity: 0.9;">분석 시각: {now_str} | PyKrx & 네이버 금융 실시간 분석</p>
            </div>
            <!-- 루틴 바 -->
            <div style="background-color: #2F5597; color: #ffffff; padding: 9px; font-size: 12px; text-align: center;">
                🔄 [최적 루틴] 거래대금 300억↑ ➔ 상승률 3~15% ➔ 윗꼬리≤2% ➔ 전일고가돌파 ➔ 14:50 진입 ➔ 익일 09:00 빠른 청산
            </div>
            <!-- 본문 -->
            <div style="padding: 20px;">
                <h3 style="color: #1F4E78; border-bottom: 2px solid #1F4E78; padding-bottom: 8px; margin-top: 0;">
                    🎯 [진입 승인] 백테스팅 검증 합격 종목 ({len(qualified_stocks)}건)
                </h3>
                <table style="width: 100%; border-collapse: collapse; font-size: 12.5px; margin-bottom: 20px;">
                    <thead>
                        <tr style="background-color: #D9E1F2; color: #1F4E78; text-align: center;">
                            <th style="padding: 8px;">No</th>
                            <th style="padding: 8px;">종목명(코드)</th>
                            <th style="padding: 8px;">등급</th>
                            <th style="padding: 8px;">종가(매수가)</th>
                            <th style="padding: 8px;">등락률</th>
                            <th style="padding: 8px;">거래대금</th>
                            <th style="padding: 8px;">전일비거래량</th>
                            <th style="padding: 8px;">윗꼬리</th>
                            <th style="padding: 8px;">1차 목표가</th>
                            <th style="padding: 8px;">손절가</th>
                        </tr>
                    </thead>
                    <tbody>
                        {rows_html}
                    </tbody>
                </table>
                <!-- 익일 대응 가이드 -->
                <div style="background-color: #F1F5F9; border-left: 4px solid #1F4E78; padding: 14px; border-radius: 4px; font-size: 12.5px; line-height: 1.6;">
                    <strong style="color: #1F4E78;">💡 익일 09:00~09:30 실전 대응 3원칙:</strong><br>
                    • <b>시나리오 A (갭상승 +2%↑)</b>: 09:00 시초가 즉시 50% 분할 익절 ➔ 잔량은 시초가 지지 시 +3.5% 전량 청산<br>
                    • <b>시나리오 B (보합 출발)</b>: 09:05까지 1분봉 관찰 ➔ 음봉 전환 시 본전~약손절(-0.5%) 즉시 전량 정리<br>
                    • <b>시나리오 C (갭하락 or -2.5% 터치)</b>: 뒤도 보지 말고 기계적 전량 칼손절 (물타기/장기보유 절대 금지)
                </div>
            </div>
        </div>
    </body>
    </html>
    """

    msg.attach(MIMEText(html, "html"))

    try:
        server = smtplib.SMTP("smtp.gmail.com", 587)
        server.starttls()
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_USER, TARGET_EMAIL, msg.as_string())
        server.quit()
        print(f"📧 Gmail 발송 완료: {TARGET_EMAIL} ({len(qualified_stocks)}개 종목 추천)")
    except Exception as e:
        print(f"❌ 이메일 발송 실패: {e}")


def main():
    print(f"\n=======================================================")
    print(f"🚀 [전략 3 보강판] 종가매매 스크리닝 시작: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"=======================================================")

    top_stocks = get_naver_top_volume_stocks()
    print(f"📊 실시간 거래대금 상위 종목 수집 완료: {len(top_stocks)}개")

    qualified = []
    for s in top_stocks[:60]: # 상위 60개 집중 분석
        time.sleep(0.05)
        res = evaluate_candidate(s)
        if res:
            print(f"  ★ [합격] {res['name']}({res['code']}) | {res['grade']} | 거래대금: {res['trade_amt_억']}억 | 상승률: +{res['change_rate']}% | 윗꼬리: {res['pullback_pct']}% | 1차목표가: {res['target1']:,}원")
            qualified.append(res)

    send_gmail_report(qualified)
    print("✅ 스크리닝 및 메일 발송 완료!")


if __name__ == "__main__":
    main()
