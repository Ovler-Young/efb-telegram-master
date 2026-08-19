def test_truncate_ellipsis(channel):
    truncate_ellipsis = channel.topic_sync.truncate_ellipsis
    short_text = "short text"
    long_text = "This is a long text. Cursus pellentesque cras maecenas hac malesuada porttitor nullam, dignissim enim feugiat placerat eget quisque, dui sem dictum fames sapien mauris. Feugiat euismod nisi donec nunc cras aliquam diam, arcu fames pretium pellentesque faucibus phasellus, in montes felis elit lacinia auctor. Commodo curae nibh donec vel ipsum sociosqu maecenas pellentesque scelerisque suspendisse blandit himenaeos rutrum ad, nec dictum porttitor non luctus fringilla feugiat volutpat adipiscing cubilia vitae lacus. Tempor iaculis facilisis maecenas quam nisl pulvinar magnis lacus, sodales porta quisque rutrum habitasse metus purus ante libero, malesuada mollis est donec cubilia accumsan parturient. Parturient libero gravida imperdiet massa praesent habitant scelerisque pellentesque mollis elit, urna quisque tellus in nostra aliquet montes natoque fermentum, condimentum enim magna odio vestibulum mauris viverra sagittis iaculis."
    assert truncate_ellipsis(short_text, len(short_text) + 10) == short_text
    truncated = truncate_ellipsis(long_text, 256)
    assert len(truncated) <= 256
    assert truncated.endswith("…")
