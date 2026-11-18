# Vận hành, lưu giữ dữ liệu và kiểm chứng

Dashboard có **System operations**: thời gian chạy, tình trạng database, worker,
dung lượng các vùng lưu trữ, camera đang chạy, p95 suy luận, lượt bỏ mẫu vì model
bận và kết quả bị loại do trễ. Biểu đồ dùng mẫu vận hành đã lưu, không phải số
liệu minh họa. p50/p95 camera tính trên tối đa 600 mẫu được chấp nhận từ lần
khởi động gần nhất; thời gian gồm giải mã JPEG, suy luận và tạo overlay, không
đo độ trễ từ cảm biến camera tới server.

`GET /api/operations` tổng hợp trạng thái hiện tại; `GET /api/operations/history`
trả các mẫu gần nhất. Mặc định lấy mẫu mỗi 10 giây, lưu 7 ngày. Dữ liệu telemetry
cũ được xóa trong SQLite và các trang trống được tái sử dụng; chưa tự VACUUM vì
thao tác đó có thể chặn database. Không xóa lịch sử sự kiện, cảnh báo hay outbox.

`GET /api/ready` trả 200 khi camera đang giám sát có quan sát xác nhận mới,
nguồn đã bật đang chạy, model/worker dùng được và dung lượng chưa tới mức nguy
cấp. Nếu chưa sẵn sàng trả 503. Đây là readiness phần mềm tại thời điểm gọi,
không chứng nhận chất lượng cảnh báo ngoài hiện trường. Video vẫn dùng được
khi chưa cấu hình camera; lúc đó hệ thống có thể ở IDLE.

## Dung lượng và lưu giữ media

Mặc định cảnh báo khi còn dưới 5 GiB hoặc 10%; nguy cấp khi dưới 1 GiB hoặc 2%.
Kiểm tra ổ chứa uploads, outputs, database và thư mục tạm multipart của Python.
Khi nguy cấp:

- Không nhận phiên camera/video mới, API trả 507.
- Camera đang chạy chuyển UNKNOWN, tạm dừng AI và thử tiếp khi đủ dung lượng.
  Xác nhận lại từ ảnh mới; không cộng thời gian tạm dừng vào xác nhận.
- Video đang tải/xuất được kiểm tra định kỳ và dừng với lỗi nếu không đủ chỗ.
  Kiểm tra trong ứng dụng không thay thế giới hạn body/temp storage của reverse
  proxy; multipart có thể đã spool dữ liệu trước khi handler nhận yêu cầu.

Chính sách mặc định:

| Media | Hết hạn sau |
|---|---:|
| Video upload / phần tải dở của phiên đã kết thúc | 30 ngày |
| Video kết quả | 30 ngày |
| Ảnh bằng chứng cảnh báo | 90 ngày |
| Cache mask của phiên đã kết thúc | 1 ngày |
| Vùng archive, nếu đã bật purge | 7 ngày từ lúc archive |

Chỉ xét tên file chuẩn gắn với analysis/alert trong DB. Không quét xóa mọi file
trong outputs: model, báo cáo đánh giá, backup và file người dùng đặt thêm được
giữ lại. Phiên running/recovering, công việc còn chạy trong RAM, sự kiện live
active/unknown và thông báo chưa sent/cancelled được bảo vệ. Video được dùng
chung với phiên đang chạy cũng được giữ.

**Refresh preview** chỉ liệt kê media đủ điều kiện. Worker bảo trì chạy mỗi
15 phút, xử lý tối đa 100 file/lượt. **Archive expired media** chạy ngay chính
sách đó. Archive chuyển file vào `data/retention_trash` với journal; đường dẫn
trước đó sẽ không còn phục vụ file cho tới khi Restore. Link, junction, đường
dẫn thoát khỏi vùng lưu trữ bị từ chối. Không di chuyển đệ quy thư mục; thao tác
move dùng cùng volume, nếu không thể move thì giữ nguyên file và báo lỗi.

**Restore** đưa file về đường dẫn gốc, không ghi đè file đang tồn tại, giữ nội
dung gốc và bảo vệ thêm 7 ngày. Sau crash, journal kiểm tra source/archive để
đối chiếu thao tác dở; trường hợp mơ hồ giữ file và báo lỗi.

Archive trên cùng ổ đĩa **chưa giải phóng dung lượng**. Xóa vĩnh viễn mặc định
tắt (`RETENTION_PURGE_ENABLED=false`). Chỉ bật khi đã quyết định thời gian lưu
và backup phù hợp; khi bật, chỉ các file trong archive đủ tuổi và không còn được
bảo vệ mới bị xóa. Không có thao tác xóa media ngay lập tức khi chạy bộ kiểm tra.

Các biến tương ứng có trong `.env.example`. API bổ sung:

```text
GET  /api/storage
POST /api/storage/maintain
POST /api/storage/{retention_id}/restore
GET  /api/operations
GET  /api/operations/history?limit=60
GET  /api/ready
```

## Kiểm tra RTSP và kênh gửi sau khi cấu hình .env

1. Điền URL vào `.env`, ví dụ biến `CAMERA_ROAD_RTSP`, cấu hình kênh gửi cần dùng.
   Không đưa thông tin xác thực vào Git hay gửi vào chat.
2. Khởi động lại `python run.py`. Tạo/lưu profile camera, chọn nguồn RTSP dùng
   tên biến đó, Save source rồi Start monitoring. Xem [hướng dẫn camera](live-cameras.md).
3. Kiểm tra cấu hình và đọc thử một ảnh; CLI không lưu ảnh camera trong preflight:

```powershell
.\.venv\Scripts\python.exe -m validation preflight --connect --output outputs/preflight-run-01
```

Lệnh dùng kết nối DB read-only. Không gửi tin nhắn nếu không có `--send-test`.
Muốn gửi một tin TEST tới người nhận đã cấu hình, chỉ định rõ kênh, ví dụ:

```powershell
.\.venv\Scripts\python.exe -m validation preflight --send-test --channel telegram --output outputs/channel-test-01
```

Kênh khác là `email` hoặc `sms`. Lệnh này thực sự gửi qua nhà cung cấp, có thể
tính phí SMS. Đây là test transport với nội dung TEST; không tạo sự kiện ngập
giả. Accepted/provider ID chưa chứng minh người nhận đã đọc hoặc nhận SMS.
Kết quả được lưu JSON/HTML; lỗi không in URL RTSP, token hoặc người nhận.

## Chạy dài hạn

Sau khi camera đã chạy, mở terminal thứ hai:

```powershell
.\.venv\Scripts\python.exe -m validation soak --url http://127.0.0.1:8000 --duration-sec 86400 --interval-sec 5 --output outputs/soak-run-01
```

Theo dõi một ngày, lưu `samples.jsonl` tăng dần và `report.json`/HTML khi hoàn
thành hoặc Ctrl+C. Báo cáo nêu thời gian yêu cầu/thực chạy, tỷ lệ đọc được server,
camera connected/UNKNOWN/confirmed, thời gian không quan sát được, độ cũ tối đa,
p95 AI lớn nhất được lấy mẫu và số lần reconnect. Availability được ước lượng
theo chu kỳ poll; không suy ra bỏ sót/cảnh báo sai chỉ từ uptime.

Chọn một thư mục output mới cho mỗi lượt; không ghi đè bằng chứng cũ. Thử cả
ngày/đêm, mưa/khô, phản chiếu, mất mạng, restart và gần ngưỡng dung lượng. Các
ca phá nguồn/đầy đĩa thực tế cần thực hiện trên môi trường kiểm chứng có backup.

## Chấm cảnh báo bằng video được gán nhãn

Chuẩn bị ảnh tham khảo và template nhãn từ một video quay ở camera mục tiêu:

```powershell
.\.venv\Scripts\python.exe -m validation prepare --video media/road-test.mp4 --source-id road-recording-01 --every-sec 10 --output outputs/review-road-01
```

Đọc video, sửa `manifest.json`: chia **toàn bộ thời gian** thành các đoạn
`flood`, `normal` hoặc `unreviewed`. Người kiểm tra dùng tiêu chí cảnh báo tại
vùng đường giám sát để gán nhãn; không dùng dự đoán RIWA làm nhãn chuẩn. Ví dụ
cấu trúc interval, với thời gian đúng theo video thật:

```json
[
  {"start_sec": 0, "end_sec": 30, "label": "normal"},
  {"start_sec": 30, "end_sec": 90, "label": "flood"},
  {"start_sec": 90, "end_sec": 120, "label": "unreviewed"}
]
```

Đặt `target_camera=true` chỉ khi đúng nguồn mục tiêu; ghi điều kiện trong
`conditions`, ví dụ `day`, `night`, `rain`, `dry`. Có thể thêm `camera` theo schema
profile để dùng đúng ROI/ngưỡng/policy. `source_id` nhóm các đoạn cùng bản ghi
gốc; không chia đoạn gần nhau của cùng nguồn sang train/test. Lệnh từ chối trùng
đường dẫn/nội dung video và nguồn chéo split. Chỉ test split được chấm kết quả.
`training_overlap` chỉ đặt `no_overlap` khi có bằng chứng về tập train; template
giữ `unknown`, không tự đoán. Hash không phát hiện mọi bản mã hóa lại cùng cảnh.

Chạy RIWA và chấm vòng đời cảnh báo:

```powershell
.\.venv\Scripts\python.exe -m validation replay --manifest outputs/review-road-01/manifest.json --interval-sec 0.5 --output outputs/event-eval-01
```

Xuất observation CSV, báo cáo từng clip và tổng hợp JSON/HTML, hash video/model/
manifest. Chỉ số gồm precision/recall sự kiện, số cảnh báo sai trên giờ footage
normal, số giây cảnh báo sai, độ trễ phát hiện p95 và tỷ lệ UNKNOWN. Ghép sự kiện
một-một khi có giao nhau trong thời gian đã gán nhãn; UNKNOWN không được tính là
quan sát an toàn. Một cảnh báo kéo dài có thể được ghép một sự kiện thật nhưng
vẫn bị tính thời lượng cảnh báo sai ở các đoạn normal.

Dùng video CFR đã kiểm chứng vì replay dùng đồng hồ nominal FPS. Đo offline
không đo độ trễ truyền RTSP hoặc gửi thông báo. Interval unreviewed không được
coi là normal; thiếu nhãn phải được ghi rõ, không lấp bằng kết quả model.

## Quyết định về model

Gate mặc định cần ít nhất 10 sự kiện thật, 1 giờ normal, đủ day/night/rain/dry,
đúng camera mục tiêu, nhãn đầy đủ và không chồng tập train. Ngưỡng ví dụ của
project: precision ≥0.90, recall ≥0.95, cảnh báo sai ≤0.1/giờ normal, delay p95
≤10 giây, UNKNOWN ≤5%. Đây là tiêu chí ban đầu có thể thay bằng JSON qua
`--acceptance`; cần thống nhất với mục đích triển khai, không phải chuẩn chung.

- `collect_more_data`: chưa đủ bằng chứng; không kết luận model cần train lại.
- `investigate_before_training`: dữ liệu đủ nhưng có chỉ số không đạt; kiểm tra
  nhãn, ROI, ngưỡng/policy và lỗi segmentation trước khi chọn fine-tune.
- `keep_current_model_candidate`: đạt gate offline; vẫn cần đối chiếu chạy dài
  hạn và nhận cảnh báo thực tế, không đồng nghĩa sẵn sàng vận hành hiện trường.

Không tự train hoặc thay checkpoint đang chạy dựa vào một lượt replay. Nếu cần
fine-tune, dùng tập train độc lập và so sánh cả model cũ/mới trên cùng test set.
Mã thoát CLI: 0 cho lượt thu/chấm đáp ứng điều kiện lệnh, 2 khi chưa đủ/không đạt,
1 cho lỗi đầu vào/I/O. Mã 0 của soak/preflight không chứng minh đạt chất lượng ngập.

## Bằng chứng kiểm thử hiện có

Đã kiểm thử retention trên file tạm biệt lập, lỗi move, restore không ghi đè,
chống thoát đường dẫn, bảo vệ công việc và mô phỏng dung lượng nguy cấp; kiểm
thử chấm sự kiện có số liệu biết trước. Edge desktop/mobile dùng RIWA thật,
archive/restore và một lượt 60 giây với hai lần ngắt packet mô phỏng. Báo cáo
soak ghi `field_acceptance=not_established`. Replay video chưa được review trả
`collect_more_data`. Đây là kiểm chứng phần mềm, chưa thay cho một ngày camera
thật, kênh gửi thật và nhãn hiện trường.
