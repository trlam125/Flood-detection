# Phục hồi phiên và gửi cảnh báo bền vững

Giai đoạn này thêm checkpoint camera và SQLite outbox. Việc lưu sự kiện, lịch
sử cảnh báo, các thông báo cần gửi và checkpoint của cùng quan sát nằm trong
một transaction. Không gọi mạng trong luồng phân tích camera hoặc video.

## Phục hồi sau khi server bị ngắt

Mỗi quan sát camera hợp lệ và lần đổi sang UNKNOWN lưu checkpoint: lịch sử/ID
sự kiện, mức đã xác nhận trước đó, thống kê và revision cấu hình. Khi khởi động,
server kiểm tra các analysis còn `running` hoặc `recovering`:

- Chỉ phục hồi camera có checkpoint hợp lệ, nguồn còn `enabled`, đã bật
  **Resume running monitoring after a server restart**, và cả revision profile
  lẫn nguồn còn khớp với phiên bị ngắt. Mỗi camera chỉ phục hồi phiên mới nhất.
- Giữ analysis ID và event ID, nhưng chuyển sự kiện đang mở về `unknown` trước
  khi đọc lại nguồn. Bộ đếm xác nhận và EMA bắt đầu lại. Không tính thời gian
  server tắt vào thời gian xác nhận, nước rút hoặc lịch nhắc lại.
- Nếu chưa mở được nguồn, phiên ở `recovering` và sự kiện UNKNOWN. Sửa lỗi biến
  môi trường/kết nối rồi Start; phiên vẫn có thể phục hồi nếu revision còn khớp.
- Nếu đã đổi cấu hình, không bật tự tiếp tục, không có checkpoint hoặc checkpoint
  không hợp lệ, đóng quan sát cũ bằng `interrupted`, không kết luận an toàn.
  Phiên video đang dở được đánh dấu lỗi, không tự chạy lại video/cảnh báo.
- Shutdown thông thường và nút Stop kết thúc phiên có kiểm soát. Sau đó Start
  hoặc auto-start tạo phiên mới. Phục hồi cùng ID áp dụng cho phiên chưa kết thúc
  do crash, không phải phiên người vận hành đã dừng.

Mốc thời gian sau phục hồi lấy giá trị lớn hơn giữa thời gian đã lưu và độ lệch
đồng hồ máy so với mốc bắt đầu phiên; không lùi thời gian nếu đồng hồ máy lùi.
Trạng thái luôn bắt đầu UNKNOWN dù khoảng gián đoạn ngắn. Giờ hiển thị là giờ
quan sát ở backend, không phải chứng nhận giờ camera thực sự chụp ảnh.

Một khóa hệ điều hành gắn với database ngăn hai server cùng chạy recovery.
Chạy một Uvicorn worker; khóa tự nhả nếu process bị chết. Không xóa file khóa
để mở server thứ hai trong lúc server thứ nhất còn hoạt động.

## Hàng đợi gửi

`notification_outbox` giữ một bản ghi cho mỗi `(transition_id, channel)`. Worker
nhận việc bằng transaction và lease có token, gia hạn lease trong lúc gửi. Kết
quả từ token đã bị thay thế không được ghi đè kết quả của worker mới.

Trạng thái trên dashboard:

| Nhãn | Ý nghĩa |
|---|---|
| Queued | Đã lưu, chờ worker hoặc chờ thông báo trước cùng camera/kênh |
| Sending | Worker đã nhận bằng lease |
| Retry scheduled | Lỗi tạm thời, đã lưu thời điểm thử lại |
| Accepted | Nhà cung cấp đã nhận yêu cầu, hoặc SMTP đã chấp nhận |
| Failed | Lỗi không nên tự thử lại, hoặc hết số lần tự thử |
| Blocked | Kênh bị tắt, thiếu cấu hình hoặc người nhận đã thay đổi |
| Skipped | Người vận hành chủ động bỏ qua thông báo này |

Email, Telegram và SMS có trạng thái riêng. Kênh thành công không bị gửi lại
khi một kênh khác lỗi. Các thông báo của cùng camera và cùng kênh được xử lý
theo thứ tự. Failed/Blocked giữ các thông báo sau của kênh đó; kênh khác vẫn chạy.

Trong **Notification delivery**, sửa nguyên nhân rồi dùng **Retry** cho thông
báo Failed/Blocked. Retry giữ ID thông báo và tổng số lần đã thử, bắt đầu một
chu kỳ retry mới. **Skip message** bỏ thông báo chưa gửi để giải phóng các thông
báo sau. Không cho Retry/Skip một thông báo đang Sending hoặc đã Accepted.
Khi chưa chắc thông báo trước đã đến người nhận hay chưa, kiểm tra trước khi Retry.

Các biến `.env` mặc định:

```dotenv
OUTBOX_MAX_ATTEMPTS=5
OUTBOX_RETRY_BASE_SEC=5
OUTBOX_RETRY_MAX_SEC=300
OUTBOX_LEASE_SEC=120
```

Mặc định thử tối đa 5 lần trong một chu kỳ, gồm lần đầu và 4 lần thử lại. Khoảng
đợi tăng theo cấp số nhân; tôn trọng thời gian chờ lớn hơn do nhà cung cấp trả về.
Lease của tiến trình bị crash được nhận lại sau khi hết hạn; không cần giữ hàng
đợi trong RAM. Thông báo lưu trước khi tắt máy vẫn được xét gửi sau khi mở lại.
Nội dung giữ thời điểm quan sát gốc để phân biệt cảnh báo cũ với trạng thái hiện tại.

Người nhận được chụp bằng fingerprint, không lưu token/mật khẩu vào outbox.
Worker chặn nếu cấu hình người nhận đã thay đổi, không chuyển backlog sang một
người nhận mới. Có thể thay mật khẩu/token mà giữ nguyên người nhận rồi Retry.
Hãy cấu hình đầy đủ kênh trước khi bật gửi ngoài cho camera/replay.

Lịch sử/cảnh báo cũ được giữ nguyên. Không tự chuyển các thông báo của phiên bản
cũ sang outbox vì không biết chắc chúng đã được gửi hay chưa.

## Giới hạn xác nhận gửi và chống trùng

Unique key, transaction và lease ngăn tạo trùng việc gửi và nhận việc đồng thời.
Vẫn có cửa sổ không thể kết luận: nhà cung cấp đã nhận, nhưng process chết hoặc
phản hồi mất trước khi ghi kết quả. Retry trong trường hợp đó có thể làm người
nhận thấy thông báo trùng. Đây là cơ chế gửi có thử lại, không cam kết exactly-once.
Email dùng Message-ID ổn định theo transition để hỗ trợ đối chiếu, không xem đó
là bảo đảm mọi mail server sẽ tự khử trùng.

Accepted không chứng minh người nhận đã đọc hoặc điện thoại đã nhận SMS.
Telegram trả trường `ok` và message ID trong [Bot API](https://core.telegram.org/bots/api).
Twilio trả Message SID và có các trạng thái queued/sent/delivered khác nhau,
theo [tài liệu Message resource](https://www.twilio.com/docs/messaging/api/message-resource).
Bước này chưa thêm webhook xác nhận giao hàng hay xác nhận của người vận hành.

Nếu ghi checkpoint/outbox thất bại, camera dừng và báo UNKNOWN/lỗi lưu trữ, giữ
checkpoint đã commit gần nhất để phục hồi sau khi sửa lỗi và khởi động lại server.
Giám sát dung lượng và chính sách archive/restore được mô tả trong
[operations-validation.md](operations-validation.md).

## API và kiểm thử

```text
GET  /api/deliveries?limit=30
POST /api/deliveries/{id}/retry
POST /api/deliveries/{id}/skip
```

API trả trạng thái, lần thử, lịch retry, lỗi đã loại bỏ thông tin nhạy cảm và ID
phản hồi nhà cung cấp. Không trả payload chứa cấu hình xác thực. Kết quả từng
kênh cũng có trong `GET /api/alerts` qua `dispatch`.

Các ca đã kiểm tra bao gồm rollback
toàn transaction, nhận việc đồng thời, lease hết hạn, retry sau mở DB lại, lỗi
người nhận, phục hồi cùng sự kiện và UNKNOWN. Có ca buộc dừng tiến trình con thật
đang giữ khóa server và một thông báo Sending, rồi mở lại để kiểm tra recovery.
Edge desktop/mobile được thử với worker thật, bộ gửi mô phỏng, nút Retry/Skip
và các luồng camera/video cũ. Chưa gửi thử tới Email/Telegram/SMS thực tế.
