# Phân tích ngay khi chọn video

Trong **Phân tích video**, chỉ cần chọn file. Hệ thống tự đọc và hiển thị kết quả
khi từng khung được phân tích, không có bước chờ tải toàn bộ file lên trước.
Nếu trình duyệt đọc được codec, các khung JPEG được gửi tới model RIWA. Nếu
không, server yêu cầu từng đoạn byte của file qua WebSocket để giải mã ở tiến
trình riêng. Cách này hỗ trợ seek tới metadata cuối file trước khi đọc hết file.

Mặc định không lưu toàn bộ bản gốc hoặc xuất video: kết quả và lịch sử được ghi
ngay khi đọc đủ mẫu. Trong **Tùy chọn video**, bật **Lưu thêm video kết quả ở nền**
nếu cần bản video tải xuống. Việc truyền file và xuất video lúc đó chạy riêng,
không chặn kết quả từng khung. Giải mã, truyền các byte cần thiết và chạy model
vẫn mất thời gian; độ trễ phụ thuộc file, mạng và máy, không cam kết 0 giây.

Khung video chính phát riêng với tốc độ phát của video, không dùng ảnh AI lấy
mẫu làm video. Nếu codec được trình duyệt hỗ trợ, video phát từ file cục bộ;
codec khác dùng luồng H.264/AAC fragmented MP4 chuyển định dạng ngay trong lúc
đọc, tối đa 1280 px/30 FPS, giới hạn 2 luồng cho từng bộ mã hóa/giải mã FFmpeg.
Không chờ chuyển xong cả file. Video tự phát sau kết quả AI đầu tiên, để model
không phải đuổi theo video trong lúc khởi động. Vùng nước và ROI được vẽ bằng
lớp PNG trong suốt trên video, theo mẫu gần nhất không vượt quá mốc đang phát.
Khi tua hoặc khi AI bị chậm, lớp phủ cũ được xóa; không dùng mask từ tương lai.
Lớp phủ giữ đúng tỷ lệ ảnh, không chặn các nút điều khiển video. Tối đa bốn ảnh
lớp phủ đã giải mã được giữ trong bộ nhớ; mẫu PNG nén vẫn được giữ để tua lại.
Ảnh phân tích đầy đủ cũng có ở **Ảnh AI mới nhất**, kèm mốc thời gian mẫu.
Video xuất tùy chọn vẫn có mặt nạ nước và đường biên ROI. Mức đã xác nhận, tỷ lệ nước,
độ tin cậy phân đoạn và trạng thái chờ xác nhận nằm ở thanh thông tin ngay bên
dưới; tiến độ cũng không che hình. Khi phát hoặc tua video kết quả, thanh thông
tin dùng mẫu phân tích đã lưu gần nhất không vượt quá thời điểm đang phát và
ghi rõ mốc thời gian mẫu. Timeline lấy mẫu thưa hơn luồng suy luận nên các chỉ
số này không được coi là phép đo mới tại từng khung phát. Video kết quả xuất
mới không có bảng chữ; file đã xuất trước thay đổi cần được phân tích lại.

## Luồng xử lý

```text
Chọn file → đọc khung tại 0s → AI → hiển thị ảnh đánh dấu
                         ↓
        tiếp tục đọc mẫu và hiển thị kết quả tự động
                         ↓
         đọc đủ mẫu → hoàn tất kết quả và lịch sử
```

Khi bật lưu video, bản gốc được truyền ở nền sau kết quả đầu tiên; quá trình xuất
video dùng cache mask khi cả khung phân tích và bản gốc đã sẵn sàng.

- **Đọc một khung mỗi (giây)** chọn khoảng lấy mẫu trong video, mặc định
  0.5 giây. Ví dụ 0, 0.5, 1, 1.5 giây. Đây không phải khoảng chờ giữa các yêu cầu.
- Mỗi thời điểm chỉ có một khung đang suy luận. Khung sau được đọc khi khung
  trước hoàn tất; máy/đường truyền chậm làm tiến độ chậm lại, không tự bỏ đoạn.
- Tiến độ phân tích/xuất kết quả và tiến độ tải bản gốc được hiển thị riêng.
- Khi đọc đủ mẫu, thống kê và biểu đồ được hiển thị ngay cả khi bản gốc còn đang
  tải hoặc video kết quả còn đang được xuất; giao diện ghi rõ đang chờ export.
- Video phát độc lập với nhịp AI. Chỉ số dùng mẫu đã phân tích ở hoặc trước vị
  trí đang phát; nếu chưa có hoặc quá cũ thì hiển thị chưa có dữ liệu. Không
  dùng trạng thái xác nhận của mẫu tương lai. Ảnh AI mới nhất có mốc riêng.

ROI, ngưỡng và chính sách thời gian của camera được chụp ở lúc mở phiên. Sửa
camera trong lúc chạy không đổi phiên đang xử lý. Không cần chọn ROI lại cho
mỗi video từ cùng góc nhìn.

## Kết quả và xuất video

Khi bật lưu video, mask của từng mẫu được lưu tạm thành PNG. Backend đọc video gốc rồi tô bằng mask
của mẫu gần nhất phía trước, resize theo độ phân giải video gốc. **Không chạy AI
lại và không tạo thêm một bộ sự kiện/cảnh báo trong lúc xuất video.**

Khung JPEG gửi lên giữ tỷ lệ ảnh, giới hạn cạnh dài 1280 px và dùng chất lượng
JPEG 94%. Do lấy mẫu, resize và nén JPEG, số liệu có thể khác chế độ backend đọc
trực tiếp bản gốc. `analysis_resolution` ghi kích thước ảnh thực sự được model
phân tích. Tỷ lệ pixel và `monitored_pixels` thuộc ảnh phân tích đó; video xuất
được giữ kích thước gốc.

Giữa hai mẫu, vùng tô có thể chưa bám chuyển động mới. Khoảng lấy mẫu nhỏ hơn
tăng số lần suy luận và dữ liệu khung hình được truyền. Video kết quả dùng H.264
và không có âm thanh, cùng quy ước với luồng xuất video trước đây.

Mặc định `done` được ghi ngay khi nhận đủ mẫu. Khi đã chọn lưu video, `done` chỉ
được ghi sau khi xuất thành công; kết quả phân tích vẫn hiện trước đó. Một phiên
chỉ có một bản ghi phân tích và một bộ sự kiện. Cache mask được xóa khi hoàn tất
hoặc hủy; chỉ lưu video gốc/kết quả khi đã chọn xuất video.

## Thời gian quan sát

Phiên đọc bằng trình duyệt dùng vị trí seek (`browser_media_seek`); bộ giải mã
native dùng chỉ số khung/FPS (`native_video_fps`). Cả hai không dùng thời gian
chờ mạng hay thời gian model chạy. Khoảng lấy mẫu phải nhỏ
hơn hoặc bằng `max_gap_sec`. Tất cả mẫu dự kiến đến gần cuối video phải được
nhận trước khi backend chấp nhận kết thúc.

Khi dựng video kết quả, backend dùng timestamp do OpenCV cung cấp nếu tăng đơn
điệu, có tính đến timestamp đầu; nếu backend không cung cấp timestamp thì ước
lượng bằng chỉ số khung/FPS. Video xuất là FPS cố định theo thông tin bản gốc;
chưa cam kết đồng bộ chính xác cho mọi file có timestamp/FPS bất thường.

## Codec và chế độ cũ

Trình duyệt được thử trước, chẳng hạn H.264 MP4. Nếu không đọc được metadata/
khung đầu trong 2,5 giây, hệ thống tự dùng bộ giải mã native qua byte-range.
Không tự chuyển sang tải cả file. Backend chấp nhận MP4/MOV/AVI/MKV/M4V.

Nếu cần luồng cũ, chọn **Tùy chọn video → Cách đọc → Tương thích**. Chế độ này
ghi rõ cần tải toàn bộ file trước. Khoảng lấy mẫu native và browser dùng cùng
đơn vị giây; native hiện dựa vào FPS danh nghĩa, cần kiểm tra riêng với video VFR.

Bộ đọc byte-range chỉ mở HTTP proxy trên loopback với đường dẫn ngẫu nhiên,
chunk tối đa 128 KiB, tối đa 4 yêu cầu chờ và 2 bộ đọc hoạt động. Tiến trình
native có timeout mở/đọc; đóng kết nối sẽ dừng tiến trình và giải phóng proxy.
Trình duyệt xác nhận đã nhận kết quả trước khi server xử lý tiếp. Không có hàng
đợi suy luận không giới hạn. Proxy/ngrok cần hỗ trợ WebSocket.

Luồng phát native đọc từng chunk 64 KiB từ FFmpeg và dùng backpressure của HTTP,
không ghi một bản chuyển định dạng đầy đủ ra đĩa. WebSocket/proxy được giữ tới
khi cả AI và truyền luồng phát hoàn tất, rồi tự giải phóng; video đã buffer vẫn
tiếp tục phát. Chọn video khác/đóng trang cũng giải phóng nguồn và tiến trình.
Trình duyệt có thể chỉ tua được vùng đã tải của luồng native.

Nếu lỗi xảy ra sau khi đã bắt đầu phiên khung, phiên đó được ghi lỗi với kết quả
một phần; không tự chạy lại toàn bộ để tránh trùng sự kiện/cảnh báo.

## Hủy, đóng tab và lỗi

- **Dừng phân tích** dừng đọc và tải bản gốc; sự kiện chưa kết thúc được giữ như
  quan sát bị gián đoạn, không được coi là đã phục hồi.
- Đóng tab gửi yêu cầu hủy nếu kết nối còn hoạt động. Phiên bị bỏ dở được dọn sau
  `PREVIEW_IDLE_TIMEOUT_SEC`, mặc định 1800 giây không có hoạt động.
- Lỗi tải bản gốc/xuất video hiển thị rõ; kết quả một phần được giữ trong job và
  database. Không công bố video đầy đủ khi export chưa thành công.
- Chưa tiếp tục phiên đang chạy sau khi backend khởi động lại. Chọn lại file để
  bắt đầu một phiên mới.

## API mới

```text
POST /api/local-readers               tạo bộ đọc byte-range khi browser không đọc codec
WS   /api/local-readers/{id}/bridge    yêu cầu byte, dữ liệu file, kết quả và ACK
GET  /api/local-readers/{id}/playback  phát H.264/AAC progressive, không đợi AI
POST /api/previews                    metadata + cấu hình, chưa gửi file
POST /api/previews/{id}/frames         JPEG nhị phân, query sequence/time_sec
POST /api/previews/{id}/original       multipart file gốc; không chạy AI lần nữa
POST /api/previews/{id}/finish         xác nhận đã gửi đủ mẫu
POST /api/previews/{id}/cancel         hủy phiên
GET  /api/jobs/{id}                    tiến độ/kết quả như các job trước
```

`export_video=false` kết thúc phiên sau khi đọc đủ mẫu và không yêu cầu bản gốc.
Giao diện mặc định gửi giá trị này. API `/api/previews` vẫn mặc định `true` để
tương thích client cũ; client mới nên gửi rõ tùy chọn. `/api/local-readers` mặc
định `false`. Bộ đọc native tạo cùng loại phiên phân tích và sử dụng cùng ROI,
policy, model, outbox và lịch sử; không có pipeline cảnh báo thứ hai.

Mỗi frame phải có sequence tăng đúng một đơn vị từ 0 và timestamp phù hợp với
khoảng lấy mẫu. Backend kiểm tra JPEG, kích thước, tỷ lệ khung hình, thứ tự và
giới hạn 3 MiB/frame. Bản gốc được kiểm tra tên, số byte, kích thước và thời lượng
với metadata đã mở phiên. Đây là kiểm tra nhất quán, không phải chứng minh mật mã
rằng từng JPEG do client gửi bắt nguồn từ bản gốc đó.

Tối đa 100.000 mẫu/phiên; video dài cần khoảng lấy mẫu phù hợp. Snapshot ghi
`analysis_mode=browser_frames`, `sample_interval_sec`, nguồn và cấu hình thực tế.

## Kiểm chứng

Các ca đã kiểm tra bao gồm kết quả có trước bản gốc, hai thứ tự hoàn tất,
số lần AI không tăng khi export, một bộ cảnh báo/lịch sử, lỗi dữ liệu, hủy, hết
hạn, lỗi model/export và camera đổi cấu hình trong khi chạy.

Kiểm tra Edge headless với RIWA thật đã chủ động giữ request upload bản gốc,
xác nhận ít nhất hai frame đánh dấu vẫn được hiển thị, sau đó tiếp tục upload
và xuất đủ video. Đã kiểm tra desktop/mobile, chế độ upload cũ và nhánh dự phòng
khi trình duyệt không giải mã được file.
