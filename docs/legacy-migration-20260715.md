# 2026-07-15 레거시 승인 상태 정리

원문 provenance가 `verified`로 확정되지 않은 기존 보유 종목은 승인 포트폴리오에서 제거하고, 최신 평가가격 기준으로 모델 원장에 행정 편출을 기록했다. 향후 원문 근거를 사람이 확인하면 관리자 큐에서 다시 심사할 수 있다. 행정 편출은 Telegram의 오늘 매매 신호가 아니다.

| 종목 | 기존 상태 | 최종 상태 | 근거 | 비중 변화 |
| --- | --- | --- | --- | --- |
| LS | legacy | pending_admin | 승인 신호 연결 확인 전 관리자 확인 | 0.60% → 0.00% |
| 대한전선 | legacy | pending_admin | 승인 신호 연결 확인 전 관리자 확인 | 0.60% → 0.00% |
| 삼성중공업 | legacy | pending_admin | 승인 신호 연결 확인 전 관리자 확인 | 0.80% → 0.00% |
| Google (Alphabet) | legacy | pending_admin | 승인 신호 연결 확인 전 관리자 확인 | 1.00% → 0.00% |
| Microsoft | legacy | pending_admin | 승인 신호 연결 확인 전 관리자 확인 | 0.60% → 0.00% |
| Alcoa | legacy | pending_admin | 승인 신호 연결 확인 전 관리자 확인 | 0.80% → 0.00% |
| Recursion Pharmaceuticals | legacy | pending_admin | 승인 신호 연결 확인 전 관리자 확인 | 0.60% → 0.00% |
| Northrop Grumman | legacy | pending_admin | 승인 신호 연결 확인 전 관리자 확인 | 0.80% → 0.00% |
| LG이노텍 | legacy | pending_admin | 승인 신호 연결 확인 전 관리자 확인 | 0.60% → 0.00% |
| Wabtec | legacy | pending_admin | 승인 신호 연결 확인 전 관리자 확인 | 0.60% → 0.00% |
| 두산로보틱스 | legacy | pending_admin | 승인 신호 연결 확인 전 관리자 확인 | 0.60% → 0.00% |
| 현대차 | legacy | pending_admin | 승인 신호 연결 확인 전 관리자 확인 | 1.00% → 0.00% |
| 네이버 | legacy | pending_admin | 승인 신호 연결 확인 전 관리자 확인 | 1.00% → 0.00% |
| 일라이 릴리 | legacy | pending_admin | 승인 신호 연결 확인 전 관리자 확인 | 1.00% → 0.00% |
| Marvell Technology | legacy | pending_admin | 승인 신호 연결 확인 전 관리자 확인 | 0.60% → 0.00% |
| SK하이닉스 | legacy | pending_admin | 연결 신호가 있어도 승인 유지 근거로 자동 승격하지 않음 | 0.80% → 0.00% |

HLB는 동일 코드(028300)의 두 관심종목 기록을 하나로 병합하고 두 thesis 이력을 보존했다.
