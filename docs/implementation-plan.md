# Tiến độ cải thiện FloodEye

Phạm vi hướng tới: giám sát một camera cố định trước, giữ video upload để kiểm
thử, mở rộng số camera sau khi có số liệu hiệu năng. Không diễn giải tỷ lệ nước
trong ảnh thành độ sâu hoặc diện tích thực tế.

| Giai đoạn | Trạng thái | Điều kiện hoàn thành |
|---|---|---|
| Đánh giá segmentation hiện tại | Đã triển khai công cụ và chạy baseline | Manifest ảnh/nhãn, kiểm tra dữ liệu, chỉ số theo nguồn, ảnh lỗi và hướng dẫn chạy lại |
| ROI và cấu hình theo camera | Đã triển khai và kiểm thử | ROI lưu được, tỷ lệ chỉ tính trong ROI, cấu hình có phiên bản |
| Vòng đời sự kiện/cảnh báo | Đã triển khai và kiểm thử | Xác nhận theo thời gian, ngưỡng tăng/giảm, nhắc lại, kết thúc, UNKNOWN |
| Phân tích ngay khi chọn video | Đã triển khai và kiểm thử | Có kết quả khung trước khi tải xong bản gốc; xuất đủ video, không chạy AI/cảnh báo hai lần |
| Camera trực tiếp | Đã triển khai, kiểm thử nguồn mô phỏng và capture native; chờ thiết bị thật | RTSP/webcam, kết nối lại, giữ ảnh mới nhất, phát hiện dữ liệu trễ |
| Lưu trạng thái và gửi thông báo | Đã triển khai và kiểm thử checkpoint/outbox | Phục hồi sự kiện về UNKNOWN, retry từng kênh, transaction/lease/token chống nhận việc trùng; chờ kiểm chứng kênh thật |
| Dashboard giám sát | Đã triển khai và kiểm thử | Live, sự kiện, gửi từng kênh, tổng hợp vận hành, telemetry, dung lượng và archive/restore |
| Fine-tune nếu cần | Đã có gate đánh giá; chờ dữ liệu mục tiêu | Replay nhãn sự kiện, kiểm tra tách nguồn, tiêu chí giữ model/điều tra lỗi/thu thêm dữ liệu; chưa thay model |
| Kiểm chứng vận hành | Đã triển khai công cụ và kiểm thử phần mềm; chờ hiện trường | Preflight/đọc thử camera/test kênh có chủ đích, soak JSONL, chấm cảnh báo sai/bỏ sót/độ trễ từ nhãn |

## Kết quả bàn giao của bước đánh giá

- Mã nguồn: `evaluation/`.
- Cách dùng và quy ước số liệu: [evaluation.md](evaluation.md).
- Tổng hợp kết quả và quyết định về model: [baseline-results.md](baseline-results.md).
- Dữ liệu chuẩn bị cục bộ: `data/evaluation/parepare/` (không đưa vào Git).

Báo cáo và ảnh kiểm tra được tạo cục bộ bằng các lệnh trong hướng dẫn đánh giá;
các file sinh ra có thể đã được dọn và không thuộc mã nguồn bàn giao.

Baseline hiện tại là đánh giá thăm dò, không chứng minh tính tổng quát trên camera
mới hoặc chất lượng cảnh báo sự kiện. Cần thêm dữ liệu không ngập, nguồn mục tiêu
và thông tin tập train trước khi chốt quyết định fine-tune.

## Giai đoạn ROI đã bàn giao

- Hướng dẫn: [camera-roi.md](camera-roi.md).
- Backend: `app/cameras.py`, `app/regions.py`, API camera và tích hợp suy luận.
- Giao diện: tạo/chọn camera, vẽ đa giác từ ảnh/video, lưu và tải lại cấu hình.
- Database: revision bất biến, snapshot cấu hình cho phân tích và liên kết cảnh báo.
- Đã kiểm tra trên Edge headless với model RIWA thật, ở màn hình desktop/mobile.

## Giai đoạn sự kiện theo thời gian đã bàn giao

- Hướng dẫn và giới hạn đồng hồ video: [temporal-events.md](temporal-events.md).
- `app/events.py`: theo dõi thời gian, hysteresis, UNKNOWN và vòng đời sự kiện.
- Lưu sự kiện/chuyển trạng thái, API xem lịch sử và giao diện chi tiết sự kiện.
- Cấu hình thời gian trong profile camera, chụp policy theo từng lượt phân tích.
- Làm mượt theo thời gian giữa các mẫu và phân biệt observed/confirmed.
- Replay mặc định dashboard-only; cấu hình riêng nếu muốn gửi ra ngoài.

Nguồn camera trực tiếp và xử lý mất/kết nối lại nguồn đã được triển khai ở bước sau.

## Phân tích ngay khi chọn video đã bàn giao

- Hướng dẫn: [immediate-video.md](immediate-video.md).
- Backend `app/preview.py`, API phiên khung trong `app/main.py`.
- Trình duyệt đọc file và hiển thị kết quả qua `app/static/preview.js`.
- Bản gốc tải ở nền; xuất video bằng cache mask, dùng cùng sự kiện và snapshot.
- Có hủy, timeout, kiểm tra thứ tự khung và dự phòng sang upload server.
- Giao diện đã thử với request tải gốc
  được giữ lại để xác nhận suy luận thực sự không chờ upload.

## Giai đoạn camera trực tiếp đã bàn giao

- Hướng dẫn cấu hình và giới hạn: [live-cameras.md](live-cameras.md).
- `app/capture.py`: đọc nguồn ở tiến trình riêng, vùng nhớ JPEG giới hạn,
  watchdog thay tiến trình khi treo; không xử lý dồn ảnh cũ.
- `app/live.py`: xác nhận liên tục, tự kết nối lại, UNKNOWN khi mất ảnh/AI trễ,
  giữ sự kiện qua reconnect, snapshot và lịch sử riêng từng phiên.
- API lưu nguồn có revision; URL RTSP chỉ ở biến môi trường. Giao diện có trạng
  thái nguồn/AI, chạy/dừng/restart và lấy ảnh camera để vẽ ROI.
- Worker gửi live đã được thay bằng SQLite outbox ở bước tiếp theo. Tùy chọn
  auto-resume chỉ tiếp tục nguồn đã bật, có thể phục hồi cùng ID sau crash.
- Đã kiểm tra native process, Edge desktop/mobile với RIWA
  thật và packet từ video mô phỏng mất/kết nối lại. Chờ thử RTSP/webcam thật.

## Giai đoạn phục hồi và outbox đã bàn giao

- Hướng dẫn: [persistence-delivery.md](persistence-delivery.md).
- Checkpoint camera được commit cùng sự kiện, alert và việc gửi từng kênh.
- Startup nhận diện phiên dang dở; khôi phục cùng ID khi nguồn/profile còn khớp
  và đã bật auto-resume. Luôn UNKNOWN, không mang bộ đếm xác nhận/EMA qua crash.
- Worker có lease/token, retry backoff, thứ tự theo camera/kênh, và không gửi lại
  kênh đã thành công. Có kiểm tra fingerprint người nhận và khóa server theo DB.
- Dashboard có Notification delivery, lỗi từng kênh và Retry/Skip.
- Đã kiểm thử rollback, worker cạnh tranh, lỗi/rate-limit, process bị buộc dừng,
  startup và hồi quy camera/video; gửi mạng dùng bộ mô phỏng trong kiểm thử.
- Còn cửa sổ gửi trùng nếu nhà cung cấp đã nhận nhưng server chưa ghi được kết
  quả; không cam kết exactly-once hoặc SMS đã tới thiết bị.

## Vận hành, retention và bộ công cụ kiểm chứng đã bàn giao

- Hướng dẫn đầy đủ: [operations-validation.md](operations-validation.md).
- System operations, readiness, lịch sử telemetry, p95 AI, trạng thái database/
  worker/camera và dung lượng các volume. Chặn phiên mới/tạm UNKNOWN khi ít chỗ.
- Chính sách tuổi media, archive có journal và Restore; bảo vệ phiên/sự kiện/
  thông báo còn mở. Không dọn model, báo cáo hay backup. Purge vĩnh viễn mặc định tắt.
- `validation preflight`, `prepare`, `replay`, `soak`: từ cấu hình tới báo cáo
  chạy dài hạn và chấm cảnh báo theo nhãn; không tạo nhãn giả hoặc tự đổi model.
- Đã chạy thử RIWA thật với nguồn packet mô phỏng 60 giây và hai lần mất nguồn,
  kiểm tra Edge desktop/mobile, archive/restore và các ca chấm chỉ số biết trước.
- Phần mềm/công cụ của plan đã triển khai. **Chưa nghiệm thu hiện trường**: người
  dùng sẽ cấu hình RTSP/kênh gửi trong `.env`; cần chạy với thiết bị/người nhận
  thật, thu video có nhãn normal/flood và báo cáo dài hạn. Gate hiện chưa có đủ
  dữ liệu để quyết định fine-tune. Không đánh dấu phần này đạt bằng số liệu mô phỏng.

## Thông tin checkpoint đã kiểm tra

`riwa_unet_resnet34_deploy.pth` nạp thành công bằng pipeline hiện tại, kích thước
đầu vào 384, ngưỡng phân đoạn khoảng 0.55. Môi trường đánh giá có PyTorch CPU,
không có CUDA. Các chỉ số có sẵn trong checkpoint là kết quả lưu từ trước, không
thay cho lần đánh giá mới.

`best_flood_level_model.pth` có `model_state_dict`, danh sách 5 lớp
`Level0`–`Level4`, `image_size=224`, `best_val_macro_f1` khoảng 0.8183. Chưa có
định nghĩa lớp và tập nhãn mức ngập để kiểm chứng; không thể đưa vào RIWA
segmentation bằng cách đổi tên file. Chưa thay model đang dùng.
