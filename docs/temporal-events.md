# Xác nhận theo thời gian và vòng đời sự kiện

Giai đoạn này thay điều kiện "2 lần suy luận liên tiếp" và "mỗi mức một cảnh báo"
bằng bộ theo dõi thời gian cho mỗi lượt phân tích video. Model RIWA và cách chọn
ROI được giữ nguyên.

## Cách sử dụng

Khởi động lại ứng dụng bằng `python run.py`. Trong **Confirmation & event timing**,
chỉnh các thời gian và lưu camera bằng **Save profile**. Sau đó upload tự bắt đầu
phân tích như trước. Nếu không chọn camera, các thiết lập trên giao diện được áp
dụng riêng cho lượt upload đó.

**Confirmed** là trạng thái đã đủ điều kiện thời gian; **Observed** là mức phân
loại từ tỷ lệ nước đã làm mượt ở lần suy luận mới nhất. **Pending** cho biết mức
đang chờ xác nhận, thời gian đã quan sát và thời gian yêu cầu.

Đầu video bắt đầu ở `UNKNOWN`, kể cả khi ảnh đầu tiên trông an toàn. Video ngắn
hơn thời gian xác nhận có thể kết thúc với `highest_severity=UNKNOWN`, không có
sự kiện. `highest_observed_severity` vẫn cho biết mức quan sát cao nhất. Chờ xác
nhận không có nghĩa là không có nước hoặc đã an toàn.

## Các thiết lập mặc định

Các giá trị này là cấu hình demo, cần kiểm chứng theo hiện trường trước khi vận hành.

| Thiết lập | Mặc định | Ý nghĩa |
|---|---:|---|
| `confirm_sec` | 3 s | Xác nhận ban đầu hoặc tăng mức sau đủ thời gian quan sát |
| `clear_sec` | 10 s | Xác nhận giảm mức hoặc rời điều kiện cảnh báo |
| `reminder_sec` | 300 s | Khoảng nhắc lại khi sự kiện tiếp diễn; 0 tắt nhắc lại |
| `hysteresis_ratio` | 0.03 | Biên thoát 3 điểm phần trăm dưới ngưỡng vào |
| `max_gap_sec` | 5 s | Khoảng cách tối đa giữa hai quan sát để giữ tính liên tục |
| `ema_time_constant_sec` | 1 s | Hằng số thời gian làm mượt tỷ lệ nước |
| `alert_min_severity` | MODERATE | Mức xác nhận tối thiểu để bắt đầu một sự kiện |

Ví dụ với ngưỡng MODERATE 20% và biên thoát 3 điểm %, sau khi đã xác nhận
MODERATE, tỷ lệ 19% chưa làm giảm trạng thái. Cần quan sát dưới 17% đủ
`clear_sec`. Sự kiện có thể kết thúc ở LOW; thông báo nói điều kiện cảnh báo đã
giảm, không khẳng định nước đã biến mất.

Biên thoát phải nhỏ hơn từng khoảng dương giữa các ngưỡng, kể cả từ 0 đến LOW,
để tránh một cấu hình khiến trạng thái không thể phục hồi sau làm mượt. Khi LOW
bằng 0, SAFE không có khoảng giá trị theo bộ ngưỡng đó.

Xác nhận tăng mức được theo dõi riêng theo từng ngưỡng. Ví dụ tỷ lệ dao động giữa
MODERATE và CRITICAL nhưng luôn trên ngưỡng MODERATE vẫn có thể xác nhận MODERATE,
thay vì liên tục mất chuỗi xác nhận vì tên mức thay đổi.

## Đồng hồ quan sát và làm mượt

Trong luồng upload toàn file cũ, thời gian được tính bằng chỉ số khung hình chia FPS mà
OpenCV cung cấp, không dùng thời gian máy thực hiện suy luận. Video cần FPS hợp
lệ; hệ thống từ chối nếu không xác định được đồng hồ hoặc khoảng lấy mẫu lớn hơn
`max_gap_sec`. Đây là ước lượng theo FPS, phù hợp với video có FPS cố định; chưa
đọc presentation timestamp riêng cho từng khung hình của video có FPS biến thiên.

Chế độ [phân tích ngay khi chọn file](immediate-video.md) dùng vị trí seek trong
trình duyệt làm thời gian quan sát, đồng thời tải bản gốc ở nền.

Thay tần suất lấy mẫu vẫn tạo sai số thời điểm tối đa theo khoảng lấy mẫu. Bộ
theo dõi không thể biết diễn biến nằm giữa hai khung hình không được phân tích.

EMA dùng `alpha = 1 - exp(-delta_time / time_constant)` để giảm phụ thuộc vào số
lần suy luận. Khi phát hiện khoảng trống quá giới hạn, lịch sử làm mượt và bộ đếm
xác nhận được đặt lại. Không tính thời gian thiếu dữ liệu là thời gian phục hồi.

Với camera trực tiếp trong giai đoạn sau, bộ `EventTracker` nhận đồng hồ quan sát
tăng đơn điệu từ nguồn; riêng giai đoạn này chưa đọc RTSP/webcam.

## Vòng đời sự kiện

1. **Started**: mức đã xác nhận đạt ít nhất `alert_min_severity`.
2. **Increased / Decreased**: mức xác nhận thay đổi trong cùng sự kiện.
3. **Reminder**: điều kiện còn tiếp diễn đủ khoảng nhắc lại. Tăng/giảm mức đặt lại
   đồng hồ nhắc lại; không gửi bù nhiều nhắc lại cho một khoảng mất quan sát.
4. **Cleared**: xác nhận mức đã xuống dưới điều kiện cảnh báo qua ngưỡng thoát.
5. Một lần tăng tiếp sau khi đã Cleared tạo ID sự kiện mới.

Các trạng thái lưu trong database:

| Trạng thái | Ý nghĩa |
|---|---|
| `active` | Sự kiện đang tiếp diễn trong phần video đang xử lý |
| `unknown` | Mất/thiếu quan sát; giữ sự kiện, chưa xác nhận phục hồi |
| `resolved` | Đã xác nhận xuống dưới điều kiện cảnh báo |
| `open_at_end` | Hết video trong khi sự kiện chưa được xác nhận kết thúc |
| `interrupted` | Phân tích bị lỗi trong lúc có sự kiện |

`data_lost`, `data_resumed`, `observation_ended` được ghi vào lịch sử để truy vết,
không gửi như thông báo ngập mới. Xử lý lỗi model/giải mã dừng lượt phân tích và
hiển thị UNKNOWN; không đổi sang SAFE. Khả năng tự kết nối lại nguồn thuộc bước
camera trực tiếp.

`started_sec` là thời điểm xác nhận bắt đầu; `first_exceedance_sec` là lúc bắt đầu
chuỗi vượt ngưỡng tối thiểu. `peak_severity` chỉ tính mức đã xác nhận.
`max_water_ratio` tính từ thời điểm xác nhận sự kiện trở đi. `last_observed_sec`
và `resolved_sec` tách biệt với thời điểm hết video.

Mỗi video là một phiên quan sát riêng. Không tự nối hai file upload thành một sự
kiện camera liên tục hoặc suy ra diễn biến giữa hai file.

## Dashboard và API

- **Highest confirmed severity**: mức cao nhất đã đủ thời gian xác nhận.
- Phần mô tả kết quả ghi thêm mức quan sát cao nhất, trạng thái cuối và số sự kiện.
- **Flood alert events**: bấm vào một sự kiện để xem các mốc theo thời gian video.
- **Recent alerts**: phân biệt bắt đầu, tăng/giảm, nhắc lại và kết thúc điều kiện.

```text
GET /api/events?limit=20
GET /api/events?analysis_id=<job_id>
GET /api/events?camera_id=<camera_id>
GET /api/events/<event_id>
```

API upload không camera có thể nhận multipart `event_policy_json` là JSON theo
bảng thiết lập. Upload gắn camera dùng chính `event_policy` trong profile, không
cho ghi đè riêng ở lượt upload. GET/PUT camera hỗ trợ trường `event_policy`.
PUT bỏ qua trường này giữ chính sách cũ; null yêu cầu dùng mặc định server.
Camera từ phiên bản cũ chưa có policy dùng mặc định server tại lúc upload; policy
thực tế vẫn được chụp vào snapshot của job và bản phân tích.

## Lưu trữ và gửi thông báo

Database thêm bảng `flood_events`, `event_transitions` và các cột liên kết sự kiện
trong `alerts`. Lịch sử cũ giữ nguyên. ID chuyển trạng thái giúp tránh xử lý lặp
cùng một thông báo trong luồng hiện tại. Sự kiện và lịch sử đã ghi tồn tại sau
restart. Checkpoint camera và việc thử gửi lại thông báo được quản lý bằng
[outbox bền vững](persistence-delivery.md).

Video replay mặc định **chỉ thông báo trên dashboard**. Muốn gửi Email/Telegram/SMS
cho replay, bật `REPLAY_EXTERNAL_ALERTS_ENABLED=true` trong `.env` cùng cấu hình
kênh tương ứng. Bộ nhắc lại dùng đồng hồ video; worker gửi từng thông báo qua
outbox sau khi chuyển trạng thái đã được lưu thành công.

Thiết lập `.env` xem trong `.env.example`. Xác nhận và nhắc lại dùng thời gian
quan sát qua `ALERT_CONFIRM_SEC`, `ALERT_CLEAR_SEC`, `ALERT_REMINDER_SEC`.
Làm mượt dùng `EMA_TIME_CONSTANT_SEC`.

## Kiểm chứng

Kiểm thử tự động bao phủ nhiễu ngắn, dao động mức cao, hysteresis, tăng/giảm, phục
hồi, nhắc lại, mất dữ liệu, khoảng trống, EOF, lỗi suy luận, thời gian không hợp
lệ, tính nhất quán ở nhiều tần suất lấy mẫu, lưu trữ và API sự kiện. Kiểm thử
pipeline dùng video tổng hợp với chuỗi mask có diễn biến đã biết; kiểm tra giao
diện bằng Edge headless còn chạy với checkpoint RIWA thật.
